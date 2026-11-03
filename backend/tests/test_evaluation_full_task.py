"""完整图、路由与授权预算的真实边界。"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS


@pytest.mark.parametrize('group', ['primary', 'backup', 'mixed'])
def test_full_task_runs_real_graph_in_child(tmp_path, group):
    from evaluation.full_task import run_case
    row = asyncio.run(run_case('G017', group, tmp_path / group))
    assert row['task_execution_status'] == 'completed' and row['report'] and row['evidence']
    assert row['check_status'] == 'pass' and row['report_quality'] == 'unknown'
    roles = [r['model_role'] for r in row['trace']]
    assert roles and roles[0] == ('backup' if group == 'backup' else 'primary')
    if group == 'mixed':
        assert row['trace'][0]['status'] == 'failed' and set(roles[1:]) == {'backup'}
    assert row['calls'] == len(row['trace'])


def grant_payload():
    from evaluation import catalog
    return dict(authorization_id='fixture-approval', dataset_hash=catalog.summary()['dataset_hash'], case_ids=['G017'],
        model='approved-model', base_url='https://approved.invalid/v1',
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), max_model_calls=40,
        max_input_bytes=20000, max_output_tokens=1000, input_price_per_million=1.0, output_price_per_million=2.0,
        currency='USD', estimated_cost_ceiling=1.0)


def test_grant_denial_and_single_use(monkeypatch):
    from evaluation.full_task import FullTaskRequest
    from evaluation.authorization import reserve
    from services.evaluation_service import EvaluationError
    body = FullTaskRequest(profile='full_task_live', case_ids=['G017'], authorization_id='fixture-approval')
    monkeypatch.delenv('SMARTBRIEF_EVALUATION_GRANT', raising=False)
    with pytest.raises(EvaluationError):
        reserve(body)
    good = grant_payload()
    monkeypatch.setenv('OPENAI_MODEL', 'approved-model')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://approved.invalid/v1')
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-not-real')
    for change in ({'expires_at': '2020-01-01T00:00:00Z'}, {'model': 'wrong'}, {'base_url': 'file:///private'},
                   {'case_ids': ['G018']}, {'estimated_cost_ceiling': 0.0001}, {'dataset_hash': 'b' * 64}):
        monkeypatch.setenv('SMARTBRIEF_EVALUATION_GRANT', json.dumps(good | change))
        with pytest.raises(EvaluationError):
            reserve(body)
    monkeypatch.setenv('OPENAI_MODEL', 'approved-model')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://approved.invalid/v1')
    monkeypatch.setenv('SMARTBRIEF_EVALUATION_GRANT', json.dumps(good))
    assert reserve(body).model == 'approved-model'
    with pytest.raises(EvaluationError):
        reserve(body)


def test_live_grant_rejects_call_budget_below_workflow_minimum(monkeypatch):
    from evaluation.full_task import FullTaskRequest
    from evaluation.authorization import FULL_TASK_MIN_MODEL_CALLS, reserve
    from services.evaluation_service import EvaluationError
    from tests.test_evaluation_live_consent import configure, consent
    configure(monkeypatch)
    body = FullTaskRequest(profile='full_task_live', case_ids=['G017'],
        live_consent=consent() | {'max_model_calls': FULL_TASK_MIN_MODEL_CALLS - 1})
    with pytest.raises(EvaluationError) as info:
        reserve(body)
    assert info.value.code == 'LIVE_EVALUATION_BUDGET_TOO_LOW'
    ok = FullTaskRequest(profile='full_task_live', case_ids=['G017'],
        live_consent=consent() | {'authorization_id': 'fixture-minimum-ok',
                                  'max_model_calls': FULL_TASK_MIN_MODEL_CALLS,
                                  'estimated_cost_ceiling': 2.0})
    assert reserve(ok).max_model_calls == FULL_TASK_MIN_MODEL_CALLS


def test_call_budget_prevents_external_call(monkeypatch):
    from evaluation.authorization import EvaluationGrant, CallBudget
    from services.evaluation_service import EvaluationError
    async def scenario():
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            return 'response'
        grant = EvaluationGrant(**(grant_payload() | {'max_model_calls': 1, 'max_input_bytes': 32}))
        budget = CallBudget(grant, create)
        with pytest.raises(EvaluationError):
            await budget(messages=[{'content': '超出字节上限' * 10}])
        assert calls == []
        assert await budget(messages=[]) == 'response'
        assert calls[0]['max_tokens'] == 1000
        with pytest.raises(EvaluationError):
            await budget(messages=[])
    asyncio.run(scenario())


def test_full_task_http_and_live_no_record():
    from main import app
    with TestClient(app, headers=ADMIN_HEADERS) as client:
        path = '/api/v2/evaluation-center/runs'
        assert client.post(path, headers=READ_HEADERS, json=dict(profile='full_task_local', case_ids=['G017'])).status_code == 403
        assert client.post(path, json=dict(profile='full_task_live', case_ids=['G017'])).status_code == 403
        assert client.get(path).json()['data']['items'] == []
        assert client.post(path, json=dict(profile='full_task_local', case_ids=['G017'], authorized=True)).status_code == 422
        response = client.post(path, json=dict(profile='full_task_local', case_ids=['G017'], group='mixed'))
        assert response.status_code == 202
        budget = response.json()['data']['metadata']['execution_budget']
        assert budget['per_call_timeout_seconds'] == 90 and budget['completion_reserve'] == 4000
        assert budget['model_max_retries'] == 0 and budget['worker_deadline_seconds'] == 180
        import time
        rid = response.json()['data']['run_id']
        end = time.monotonic() + 40
        while time.monotonic() < end:
            row = client.get(path + '/' + rid).json()['data']
            if row['execution_status'] != 'running':
                break
            time.sleep(.05)
        assert row['execution_status'] == 'completed' and row['results'][0]['calls'] > 0
        assert row['metadata']['model_group'] == 'mixed' and row['report_quality'] == 'unknown'


@pytest.mark.parametrize('mode', ['primary', 'backup', 'mixed', 'live', 'live_failure'])
def test_worker_provider_boundary_without_network(tmp_path, monkeypatch, mode):
    from evaluation.full_task_worker import execute
    from services.usage_service import usage_context
    counters = {}
    async def supplied(**kwargs):
        if mode == 'live_failure':
            raise TimeoutError('私人错误不进入结果')
        agent = usage_context.get()[1]
        counters[agent] = counters.get(agent, 0) + 1
        if agent == 'planner':
            text = '[{"id":1,"agent":"researcher","task":"资料","depends_on":[]},{"id":2,"agent":"writer","task":"简报","depends_on":[1]}]'
        elif agent == 'researcher':
            text = '{"action":"web_search","query":"资料"}' if counters[agent] == 1 else '{"action":"done"}'
        elif agent == 'reviewer':
            text = '{"passed":true,"comments":[]}'
        else:
            text = '## 简报\n\n给定资料概要。[S1]'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))], usage=None)
    def sdk(**kwargs):
        assert kwargs['base_url'] == 'https://approved.invalid/v1' and kwargs['max_retries'] == 0
        async def close():
            await kwargs['http_client'].aclose()
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=supplied)), close=close)
    monkeypatch.setattr('openai.AsyncOpenAI', sdk)
    with patch.dict(os.environ):
        if mode.startswith('live'):
            os.environ.update(SMARTBRIEF_EVALUATION_GRANT=json.dumps(grant_payload()), OPENAI_API_KEY='fixture-not-real')
        else:
            os.environ.pop('SMARTBRIEF_EVALUATION_GRANT', None)
        asyncio.run(execute(tmp_path, 'G017', 'primary' if mode.startswith('live') else mode))
        assert os.environ['MODEL_TIMEOUT_SECONDS'] == '90'
        assert os.environ['MODEL_COMPLETION_RESERVE'] == '4000'
        assert os.environ['MODEL_MAX_RETRIES'] == '0'
    result = json.loads((tmp_path / 'result.json').read_text('utf8'))
    if mode == 'live_failure':
        assert result['check_status'] == 'fail' and '私人错误' not in str(result)
    else:
        assert result['task_execution_status'] == 'completed'
    if mode.startswith('live'):
        assert result['input_tokens'] is None and result['output_tokens'] is None


def test_full_task_input_and_child_failure(tmp_path, monkeypatch):
    from evaluation.full_task import FullTaskRequest, run_case
    for body in ({'case_ids': ['G017', 'G017']}, {'case_ids': ['G002']},
                 {'case_ids': ['G017'], 'authorization_id': 'cannot-use-for-local'}):
        with pytest.raises(ValueError):
            FullTaskRequest.model_validate(body)
    with pytest.raises(ValueError):
        asyncio.run(run_case('bad', 'primary', tmp_path))
    async def broken(*args, **kwargs):
        raise OSError('私人进程路径')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', broken)
    result = asyncio.run(run_case('G017', 'primary', tmp_path))
    assert result['check_status'] == 'unknown' and result['calls'] is None


def test_grant_storage_failure_and_expired_call(tmp_path, monkeypatch):
    from evaluation import authorization
    from evaluation.full_task import FullTaskRequest
    from services.evaluation_service import EvaluationError
    good = grant_payload()
    monkeypatch.setenv('OPENAI_MODEL', good['model'])
    monkeypatch.setenv('OPENAI_BASE_URL', good['base_url'])
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-not-real')
    monkeypatch.setenv('SMARTBRIEF_EVALUATION_GRANT', json.dumps(good))
    blocker = tmp_path / 'blocked'
    blocker.write_text('不是目录')
    monkeypatch.setattr(authorization.repository, 'root', lambda: blocker)
    with pytest.raises(EvaluationError, match='预留失败'):
        authorization.reserve(FullTaskRequest(profile='full_task_live', case_ids=['G017'], authorization_id=good['authorization_id']))
    monkeypatch.setattr(authorization.repository, 'root', lambda: tmp_path / 'write-failure')
    def broken_write(*args, **kwargs):
        raise OSError('授权写入错误')
    monkeypatch.setattr(authorization.json, 'dump', broken_write)
    with pytest.raises(EvaluationError, match='预留失败'):
        authorization.reserve(FullTaskRequest(profile='full_task_live', case_ids=['G017'], authorization_id=good['authorization_id']))


def test_worker_rechecks_grant_before_client(tmp_path, monkeypatch):
    from evaluation.full_task_worker import execute
    with patch.dict(os.environ):
        os.environ.update(SMARTBRIEF_EVALUATION_GRANT=json.dumps(grant_payload() | {'case_ids': ['G018']}),
                          OPENAI_API_KEY='fixture-not-real')
        def forbidden(**kwargs):
            raise AssertionError('不匹配授权不得创建客户端')
        monkeypatch.setattr('openai.AsyncOpenAI', forbidden)
        with pytest.raises(ValueError, match='授权不匹配'):
            asyncio.run(execute(tmp_path, 'G017', 'primary'))


def test_live_service_uses_reserved_grant_without_real_provider(monkeypatch):
    from main import app
    from services import evaluation_run_service
    from evaluation.full_task import FullTaskResult
    good = grant_payload()
    for key, value in dict(OPENAI_MODEL=good['model'], OPENAI_BASE_URL=good['base_url'],
                           OPENAI_API_KEY='fixture-not-real', SMARTBRIEF_EVALUATION_GRANT=json.dumps(good)).items():
        monkeypatch.setenv(key, value)
    received = []
    async def supplied(cid, group, directory, grant):
        received.append(grant.authorization_id)
        return FullTaskResult(case_id=cid, check_status='unknown', task_execution_status='failed', business_quality='unknown',
            report='', report_sha256='', evidence=[], trace=[], calls=None, input_tokens=None, output_tokens=None,
            duration_seconds=0.0, error='替换外部执行边界，仅验证授权传递').model_dump()
    monkeypatch.setattr(evaluation_run_service, 'full_case', supplied)
    with TestClient(app, headers=ADMIN_HEADERS) as client:
        body = dict(profile='full_task_live', case_ids=['G017'], authorization_id=good['authorization_id'])
        response = client.post('/api/v2/evaluation-center/runs', json=body)
        assert response.status_code == 202
        rid = response.json()['data']['run_id']
        import time
        for _ in range(100):
            row = client.get('/api/v2/evaluation-center/runs/' + rid).json()['data']
            if row['execution_status'] != 'running':
                break
            time.sleep(.01)
        assert received == [good['authorization_id']] and row['metadata']['mode'] == 'online'
        budget = row['metadata']['execution_budget']
        assert budget['per_call_timeout_seconds'] == 90 and budget['worker_deadline_seconds'] == 900
        assert budget['completion_reserve'] == 4000 and budget['model_max_retries'] == 0
        assert row['report_quality'] == 'unknown' and row['summary']['unknown'] == 1
        assert client.post('/api/v2/evaluation-center/runs', json=body).status_code == 409


@pytest.mark.parametrize('outcome', ['valid_live', 'exit_error', 'wrong_case', 'timeout'])
def test_child_env_and_incomplete_output(tmp_path, monkeypatch, outcome):
    from evaluation import full_task
    from evaluation.authorization import EvaluationGrant
    from services.evaluation_service import EvaluationError
    stopped = []
    class Process:
        returncode = None
        async def wait(self):
            if outcome == 'timeout':
                raise TimeoutError
            self.returncode = 2 if outcome == 'exit_error' else 0
            return self.returncode
    process = Process()
    async def spawn(*args, **kwargs):
        env = kwargs['env']
        assert env['PYTHON_DOTENV_DISABLED'] == '1' and env['TEMP'] == str(tmp_path)
        assert env.get('BACKUP_OPENAI_API_KEY') is None
        if outcome == 'valid_live':
            assert env['OPENAI_API_KEY'] == 'fixture-not-real' and env['SMARTBRIEF_EVALUATION_GRANT']
        (tmp_path / 'result.json').write_text(json.dumps(dict(case_id='G018' if outcome == 'wrong_case' else 'G017',
            check_status='unknown', task_execution_status='failed', business_quality='unknown', report='', report_sha256='',
            evidence=[], trace=[], calls=None, input_tokens=None, output_tokens=None, duration_seconds=0.1, error='测试基础设施未知')))
        return process
    async def stop(owned):
        assert owned is process
        stopped.append(owned)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(full_task, 'stop_owned_process', stop)
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-not-real')
    grant = EvaluationGrant(**grant_payload()) if outcome == 'valid_live' else None
    result = asyncio.run(full_task.run_case('G017', 'primary', tmp_path, grant))
    assert result['check_status'] == 'unknown'
    assert len(stopped) == int(outcome == 'timeout')
    async def expired(**kwargs):
        raise AssertionError('过期不得调用')
    budget = __import__('evaluation.authorization', fromlist=['CallBudget']).CallBudget(
        EvaluationGrant(**(grant_payload() | {'expires_at': '2020-01-01T00:00:00Z'})), expired)
    with pytest.raises(EvaluationError):
        asyncio.run(budget(messages=[]))


def test_full_task_effective_policy_and_deadline_selection():
    from evaluation import full_task, full_task_worker
    from services import evaluation_run_service
    assert full_task_worker.EVAL_MODEL_TIMEOUT_SECONDS == 90
    assert full_task_worker.EVAL_COMPLETION_RESERVE == 4000
    assert full_task_worker.EVAL_MODEL_MAX_RETRIES == 0
    assert full_task.deadline_seconds(None) == 180
    assert full_task.deadline_seconds(object()) == 900
    budget = evaluation_run_service.full_task_budget(None)
    assert budget == dict(max_task_tokens=500000, completion_reserve=4000,
                          per_call_timeout_seconds=90, model_max_retries=0, worker_deadline_seconds=180)
    live = evaluation_run_service.full_task_budget(grant_payload())
    assert live['worker_deadline_seconds'] == 900 and live['per_call_timeout_seconds'] == 90
    assert 'estimated_cost_ceiling' in live
