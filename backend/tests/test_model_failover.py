"""主备接管的真实预算账本与持久化边界；供应商仅使用离线替身。"""
import asyncio
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APIConnectionError, BadRequestError, AuthenticationError

from db import database, usage_repository, task_repository
from llm import client
from services.budget import ModelPolicy, completion_reserve_for
from services.execution_context import model_deadline
from services.usage_service import usage_scope, UsagePersistenceError

real_backup_factory = client.get_backup_client


def sdk(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def response():
    return SimpleNamespace(usage={'prompt_tokens': 2, 'completion_tokens': 1})


@pytest.fixture
def providers(monkeypatch):
    database.init_db()
    monkeypatch.setenv('MODEL_FAILOVER_ENABLED', 'true')
    monkeypatch.setenv('BACKUP_OPENAI_BASE_URL', 'https://backup.invalid/v1')
    monkeypatch.setenv('BACKUP_OPENAI_API_KEY', 'fixture-backup')
    monkeypatch.setenv('BACKUP_OPENAI_MODEL', 'backup-fixture')
    monkeypatch.setenv('OPENAI_MODEL', 'primary-fixture')
    monkeypatch.setenv('MODEL_FAILOVER_TOTAL_SECONDS', '1')
    monkeypatch.setenv('MODEL_CIRCUIT_COOLDOWN_SECONDS', '0.02')
    primary, backup = AsyncMock(return_value=response()), AsyncMock(return_value=response())
    monkeypatch.setattr(client, 'get_client', lambda: sdk(primary))
    monkeypatch.setattr(client, 'get_backup_client', lambda: sdk(backup), raising=False)
    from services import model_routing
    if hasattr(model_routing, 'reset_routing'):
        model_routing.reset_routing()
    yield primary, backup
    if hasattr(model_routing, 'reset_routing'):
        model_routing.reset_routing()


@usage_scope('writer', task=True)
async def invoke(task_id):
    return await client.chat([{'role': 'user', 'content': 'fixture'}])


def connection_error():
    return APIConnectionError(request=httpx.Request('POST', 'https://primary.invalid'))


def test_primary_and_backup_send_independent_limits(providers, monkeypatch):
    primary, backup = providers
    monkeypatch.setenv('MODEL_FAILOVER_TOTAL_SECONDS', '600')
    monkeypatch.setenv('PRIMARY_MODEL_COMPLETION_RESERVE', '4000')
    monkeypatch.setenv('PRIMARY_MODEL_TIMEOUT_SECONDS', '30')
    monkeypatch.setenv('BACKUP_MODEL_COMPLETION_RESERVE', '8000')
    monkeypatch.setenv('BACKUP_MODEL_TIMEOUT_SECONDS', '40')
    primary.side_effect = connection_error()
    asyncio.run(invoke('role-limits'))
    assert primary.call_args.kwargs['max_tokens'] == 4000
    assert primary.call_args.kwargs['timeout'] == 30
    assert backup.call_args.kwargs['max_tokens'] == 8000
    assert backup.call_args.kwargs['timeout'] == 40


@usage_scope('writer', task=True)
async def invoke_deadline(task_id, seconds):
    token = model_deadline.set(monotonic() + seconds)
    try:
        return await client.chat([{'role': 'user', 'content': 'fixture'}])
    finally:
        model_deadline.reset(token)


def test_glm_output_reserve_is_capped(monkeypatch):
    policy = ModelPolicy(completion_reserve=32768)
    assert completion_reserve_for('GLM-5.3-Flash', policy) == 16384
    assert completion_reserve_for('qwen3.7-flash', policy) == 32768
    monkeypatch.setenv('GLM_MODEL_MAX_OUTPUT_TOKENS', '8192')
    assert completion_reserve_for('GLM-5.3-Flash', policy) == 8192


def test_step_budgets_are_raised():
    from agents.scheduler import AGENT_STEP_TIMEOUT_SECONDS, WRITER_STEP_TIMEOUT_SECONDS
    assert AGENT_STEP_TIMEOUT_SECONDS >= 300
    assert WRITER_STEP_TIMEOUT_SECONDS >= 900


def test_step_budget_timeout_uses_transient_backup_without_binding(providers):
    primary, backup = providers
    primary.side_effect = asyncio.TimeoutError()
    asyncio.run(invoke_deadline('budget-limited', 0.2))
    assert backup.await_count == 1
    from db.model_route_repository import get_route
    assert get_route('budget-limited') is None
    # 下一次调用仍先试探主模型，而不是永久固定备用。
    primary.side_effect = None
    asyncio.run(invoke_deadline('budget-limited', 10))
    assert primary.await_count == 2


def test_replace_logs_persists_without_running_state(tmp_path):
    database.init_db(tmp_path / 'paused-logs.db')
    task_repository.create_task('paused-logs', '主题')
    task_repository.update_task('paused-logs', status='paused', execution_status='paused')
    task_repository.replace_logs('paused-logs', [
        {'type': 'Thought', 'content': '停止在 writer', 'step_id': 4},
    ])
    logs = task_repository.get_logs('paused-logs')
    assert logs and logs[0]['content'] == '停止在 writer'


def test_switch_stays_on_backup_after_recovery_and_reopen(providers):
    primary, backup = providers
    primary.side_effect = connection_error()
    asyncio.run(invoke('sticky'))
    database.init_db()
    from services.model_routing import reset_routing
    reset_routing()  # 模拟进程重启后的健康状态清空，任务绑定必须仍生效。
    primary.side_effect = None
    asyncio.run(invoke('sticky'))
    assert primary.await_count == 1
    assert backup.await_count == 2
    rows = usage_repository.list_usage('sticky')
    assert [r['model'] for r in rows] == ['primary-fixture', 'backup-fixture', 'backup-fixture']
    assert rows[0]['logical_call_id'] == rows[1]['logical_call_id'] != rows[2]['logical_call_id']
    assert [r['model_role'] for r in rows] == ['primary', 'backup', 'backup']
    from db.model_route_repository import get_route
    assert get_route('sticky')['role'] == 'backup'


def test_next_task_probes_primary_after_cooldown(providers):
    primary, backup = providers
    async def scenario():
        primary.side_effect = connection_error()
        await invoke('first')
        primary.side_effect = None
        await asyncio.sleep(0.03)
        await invoke('next')
        await invoke('first')
    asyncio.run(scenario())
    assert primary.await_count == 2 and backup.await_count == 2


@pytest.mark.parametrize('kind', ['cancel', 'bad_request', 'budget'])
def test_control_and_request_errors_never_switch(providers, monkeypatch, kind):
    primary, backup = providers
    expected = Exception
    if kind == 'cancel':
        primary.side_effect = asyncio.CancelledError()
        expected = asyncio.CancelledError
    elif kind == 'bad_request':
        request = httpx.Request('POST', 'https://primary.invalid')
        primary.side_effect = BadRequestError('PRIVATE', response=httpx.Response(400, request=request), body=None)
    else:
        monkeypatch.setenv('MAX_TASK_TOKENS', '1')
        expected = UsagePersistenceError
    with pytest.raises(expected):
        asyncio.run(invoke('blocked'))
    backup.assert_not_awaited()


def test_primary_auth_failure_can_switch(providers):
    primary, backup = providers
    request = httpx.Request('POST', 'https://primary.invalid')
    primary.side_effect = AuthenticationError('PRIVATE', response=httpx.Response(401, request=request), body=None)
    asyncio.run(invoke('auth'))
    assert primary.await_count == backup.await_count == 1


def test_both_unavailable_pause_workflow_without_losing_checkpoint(providers):
    from graph.workflow import run_task
    from services.model_routing import ModelUnavailable
    from memory.checkpointer import resumable_mode
    primary, backup = providers
    primary.side_effect = backup.side_effect = connection_error()
    task_repository.create_task('paused-model', '依赖不可用')
    with pytest.raises(ModelUnavailable):
        asyncio.run(run_task('paused-model', '依赖不可用'))
    task = task_repository.get_task('paused-model')
    assert task['execution_status'] == 'paused' and task['error_code'] == 'MODEL_UNAVAILABLE'
    assert resumable_mode('paused-model') == 'checkpoint'
    assert primary.await_count == backup.await_count == 1


def test_paused_task_persists_progress_logs(providers):
    from graph.workflow import run_task
    from services.model_routing import ModelUnavailable
    primary, backup = providers
    calls = {'count': 0}

    async def flaky(**kwargs):
        calls['count'] += 1
        if calls['count'] == 1:
            return response()
        raise connection_error()

    primary.side_effect = flaky
    backup.side_effect = connection_error()
    task_repository.create_task('paused-with-logs', '依赖不可用')
    with pytest.raises(ModelUnavailable):
        asyncio.run(run_task('paused-with-logs', '依赖不可用'))
    task = task_repository.get_task('paused-with-logs')
    assert task['execution_status'] == 'paused'
    assert task_repository.get_logs('paused-with-logs')


@pytest.mark.parametrize('name,value', [
    ('BACKUP_OPENAI_API_KEY', ''), ('BACKUP_OPENAI_BASE_URL', 'file:///private'),
    ('BACKUP_OPENAI_BASE_URL', 'https://user:password@example.invalid'),
    ('MODEL_FAILOVER_TOTAL_SECONDS', 'nan'), ('MODEL_FAILOVER_MAX_ATTEMPTS', '1'),
])
def test_invalid_failover_configuration_blocks_before_call(providers, monkeypatch, name, value):
    primary, backup = providers
    monkeypatch.setenv(name, value)
    with pytest.raises(UsagePersistenceError, match='配置无效'):
        asyncio.run(invoke('invalid'))
    assert primary.await_count == backup.await_count == 0


@pytest.mark.parametrize('operation', ['read', 'write', 'cancel_write'])
def test_binding_failure_never_calls_backup(providers, monkeypatch, operation):
    from db import model_route_repository as routes
    primary, backup = providers
    primary.side_effect = connection_error()
    def broken(*args):
        if operation == 'cancel_write':
            raise asyncio.CancelledError
        raise RuntimeError('PRIVATE')
    monkeypatch.setattr(routes, 'get_route' if operation == 'read' else 'bind_backup', broken)
    with pytest.raises(asyncio.CancelledError if operation == 'cancel_write' else UsagePersistenceError):
        asyncio.run(invoke('binding'))
    backup.assert_not_awaited()


def test_corrupt_route_cannot_cross_task(providers):
    from db.model_route_repository import get_route
    from services.model_routing import ModelRoute
    route = ModelRoute(task_id='other', model='fixture', provider='fixture', reason='MODEL_AUTH', switched_at='fixture')
    with database.get_connection() as conn:
        conn.execute('INSERT INTO task_model_routes VALUES (?,?)', ('owner', route.model_dump_json()))
    with pytest.raises(ValueError, match='归属'):
        get_route('owner')


def test_circuit_one_probe_and_stale_success_cannot_close_new_failure(providers):
    from services.model_routing import Circuit
    circuit = Circuit()
    old = circuit.acquire()
    circuit.finish(old, False, 60)
    circuit.finish(old, True, 60)
    assert circuit.acquire() is None
    circuit.until = -1
    ticket = circuit.acquire()
    assert ticket is not None and circuit.acquire() is None
    circuit.abandon(old)
    assert circuit.acquire() is None
    circuit.abandon(ticket)
    assert circuit.acquire() == ticket
    circuit.finish(ticket, True, 60)
    assert circuit.acquire() is not None


def test_concurrent_new_tasks_have_one_recovery_probe(providers):
    from services.model_routing import primary_circuit
    primary, backup = providers
    async def scenario():
        primary.side_effect = connection_error()
        await invoke('first')
        primary_circuit().until = -1
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(**kwargs):
            started.set()
            await release.wait()
            return response()
        primary.side_effect = slow
        probe = asyncio.create_task(invoke('probe'))
        await started.wait()
        await invoke('concurrent')
        release.set()
        await probe
        await invoke('first')
    asyncio.run(scenario())
    assert primary.await_count == 2 and backup.await_count == 3


def test_outer_deadline_reserves_time_for_backup(providers):
    from time import monotonic
    from services.execution_context import deadline_scope, model_deadline
    primary, backup = providers
    async def slow(**kwargs):
        await asyncio.sleep(1)
    primary.side_effect = slow
    async def scenario():
        with deadline_scope(monotonic() + 0.15):
            with deadline_scope(monotonic() + 10):
                await invoke('deadline')
        assert model_deadline.get() is None
    asyncio.run(scenario())
    assert 0 < primary.call_args.kwargs['timeout'] < 0.1
    assert backup.await_count == 1


def test_expired_deadline_stops_without_request(providers):
    from services.execution_context import deadline_scope
    from services.model_routing import ModelUnavailable
    primary, backup = providers
    async def scenario():
        with deadline_scope(0):
            await invoke('expired')
    with pytest.raises(ModelUnavailable):
        asyncio.run(scenario())
    assert primary.await_count == backup.await_count == 0


def test_missing_primary_configuration_can_use_backup(providers, monkeypatch):
    _, backup = providers
    def missing():
        raise RuntimeError('未配置主模型')
    monkeypatch.setattr(client, 'get_client', missing)
    asyncio.run(invoke('missing'))
    assert backup.await_count == 1


def test_unscoped_tools_still_failover_without_creating_binding(providers):
    primary, backup = providers
    primary.side_effect = connection_error()
    asyncio.run(client.chat([], tools=[{'type': 'function'}], tool_choice='auto'))
    assert backup.await_count == 1
    assert backup.call_args.kwargs['tool_choice'] == 'auto'
    with database.get_connection() as conn:
        assert conn.execute('SELECT count(*) FROM task_model_routes').fetchone()[0] == 0


def test_offline_context_bypasses_both_real_routes(providers):
    from evaluation.context import evaluation_context
    primary, backup = providers
    offline = AsyncMock(return_value=response())
    async def scenario():
        token = evaluation_context.set(SimpleNamespace(model='offline-fixture', create=offline, case={'scenario':'normal'}))
        try:
            await invoke('offline')
        finally:
            evaluation_context.reset(token)
    asyncio.run(scenario())
    assert offline.await_count == 1
    assert primary.await_count == backup.await_count == 0


def test_backup_sdk_has_independent_credentials_and_no_retries(providers, monkeypatch):
    monkeypatch.setattr(client, '_backup_client', None)
    # 使用原始工厂构建客户端，不发网络请求。
    actual = real_backup_factory()
    assert actual.api_key == 'fixture-backup'
    assert str(actual.base_url) == 'https://backup.invalid/v1/'
    assert actual.max_retries == 0
    assert real_backup_factory() is actual


def test_v3_upgrade_retains_reports_and_creates_empty_binding_table(providers):
    task_repository.create_task('old', '旧任务')
    with database.get_connection() as conn:
        conn.execute('DROP TABLE task_model_routes')
        conn.execute('DELETE FROM schema_migrations WHERE version=4')
        conn.execute('PRAGMA user_version=3')
    database.init_db()
    database.init_db()
    assert task_repository.get_task('old')['topic'] == '旧任务'
    with database.get_connection() as conn:
        assert conn.execute('SELECT count(*) FROM task_model_routes').fetchone()[0] == 0
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 4


@pytest.mark.parametrize('status', [408, 429, 500, 503, 403, 404])
def test_supplier_status_failures_switch_once(providers, status):
    from openai import APIStatusError
    primary, backup = providers
    request = httpx.Request('POST', 'https://primary.invalid')
    primary.side_effect = APIStatusError('PRIVATE', response=httpx.Response(status, request=request), body=None)
    asyncio.run(invoke('status'))
    assert primary.await_count == backup.await_count == 1


def test_budget_exhausted_by_failed_primary_blocks_backup(providers, monkeypatch):
    from services.budget import BudgetExceeded
    primary, backup = providers
    monkeypatch.setenv('MAX_TASK_TOKENS', '3000')
    monkeypatch.setenv('MODEL_COMPLETION_RESERVE', '2000')
    primary.side_effect = connection_error()
    with pytest.raises(BudgetExceeded):
        asyncio.run(invoke('budget-after-failure'))
    assert primary.await_count == 1 and backup.await_count == 0
    assert usage_repository.list_usage('budget-after-failure')[-1]['status'] == 'budget_exceeded'


def test_usage_persistence_failure_prevents_failover(providers, monkeypatch):
    primary, backup = providers
    primary.side_effect = connection_error()
    def broken(*args):
        raise RuntimeError('PRIVATE')
    monkeypatch.setattr(usage_repository, 'save', broken)
    with pytest.raises(UsagePersistenceError):
        asyncio.run(invoke('ledger-error'))
    backup.assert_not_awaited()


def test_service_resumes_model_pause_on_backup_only(providers):
    from services.task_service import TaskService
    from tests.fakes import ScriptedLLM
    primary, backup = providers
    primary.side_effect = backup.side_effect = connection_error()
    async def scenario():
        service = TaskService()
        created = await service.submit('恢复供应商')
        task_id = created['task_id']
        paused = await service.wait(task_id)
        assert paused['status'] == 'paused' and paused['error_code'] == 'MODEL_UNAVAILABLE'
        primary.side_effect = None
        backup.side_effect = None
        # 恢复到真实模型入口；规划、审核与补研究提供可控响应，搜索不联网。
        scripted = ScriptedLLM([
            '[{"id":1,"agent":"writer","task":"写","depends_on":[]}]',
            '{"passed": false, "comments": ["缺乏数据，请补充具体数据点及来源"]}',
            '{"action":"done"}',
            '{"passed": false, "comments": ["仍缺数据"]}',  # 收敛门禁在第二轮停止
        ])
        async def restored(**kwargs):
            try:
                return await scripted.chat(kwargs['messages'])
            except AssertionError:
                # Phase 3 起审核每轮都会调用模型；脚本耗尽时用固定拒绝响应收尾，
                # 仍由确定性规则保持 rejected/forced，不伪造质量通过。
                return await ScriptedLLM(
                    ['{"passed": false, "comments": ["仍缺数据"]}']
                ).chat(kwargs['messages'])
        backup.side_effect = restored
        await service.resume(task_id)
        final = await service.wait(task_id)
        await service.close()
        assert final['status'] == 'completed'
        assert final['quality_status'] == 'rejected' and final['forced_output']
        assert primary.await_count == 1 and backup.await_count >= 3
        assert not scripted.outputs
    # 零证据写作和审核保持缺口拒绝；补研究明确done，不发搜索。
    asyncio.run(scenario())


def test_route_contract_and_first_binding_are_immutable(providers):
    import json
    from pathlib import Path
    import jsonschema
    from db.model_route_repository import bind_backup
    from services.model_routing import ModelRoute
    first = ModelRoute(task_id='bound', model='first', provider='fixture', reason='MODEL_CONNECTION', switched_at='fixture')
    assert bind_backup(first)['model'] == 'first'
    assert bind_backup(first.model_copy(update={'model':'second'}))['model'] == 'first'
    schema = json.loads((Path(__file__).resolve().parents[2] / 'docs/schemas/contracts.json').read_text('utf-8'))
    target = dict(schema, **{'$ref':'#/$defs/ModelRoute'})
    jsonschema.validate(first.model_dump(), target)
    for change in ({'role':'primary'}, {'api_key':'PRIVATE'}, {'reason':'raw exception'}):
        assert list(jsonschema.Draft202012Validator(target).iter_errors(first.model_dump() | change))
