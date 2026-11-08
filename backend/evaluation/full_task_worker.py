"""独立工作进程；只替换供应商边界，真实图、路由和账本保持。"""
import asyncio
from collections import Counter
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# 评测沿用产品默认的单次模型超时（backend/.env.example 为 90 秒）。
# 30 秒低于 qwen 思考模型一次完整章节的生成耗时，会把写作统一打成 MODEL_TIMEOUT。
EVAL_MODEL_TIMEOUT_SECONDS = 90
EVAL_COMPLETION_RESERVE = 4000
EVAL_MODEL_MAX_RETRIES = 0


async def execute(directory, case_id, group):
    os.environ.update(PYTHON_DOTENV_DISABLED='1', DATA_DIR=str(directory / 'runtime'),
        ANONYMIZED_TELEMETRY='False', OTEL_SDK_DISABLED='true', SMARTBRIEF_RAG_SEED='0')
    live = bool(os.environ.get('SMARTBRIEF_EVALUATION_GRANT'))
    if not live:
        from tests.offline_guard import install
        install()
    from db import database, task_repository, usage_repository
    from db.model_route_repository import bind_backup
    from evaluation import catalog
    from evaluation.full_task import FullTaskResult
    from evaluation.report_grading import digest
    from graph.workflow import run_task
    from llm import client
    from memory import checkpointer, long_term_memory
    from rag import store
    from agents import react_researcher
    from skills import skills_manager
    from tools import builtin_tools
    from services.model_routing import ModelRoute, reset_routing
    from services.usage_service import usage_context
    detail = catalog.detail(case_id)
    sources = detail['sources']  # 不读取case.oracle；供应商只能看到公开题干与给定资料。
    topic = detail['case']['input']['user_request']
    feedback = os.environ.get('SMARTBRIEF_EVAL_PUBLIC_FEEDBACK', '')
    if feedback:
        topic += '\n本次公开状态诊断反馈：' + feedback
    database.DB_PATH = directory / 'tasks.db'
    checkpointer.CHECKPOINT_DB = directory / 'checkpoints.db'
    database.init_db()
    tid = 'full-' + case_id
    task_repository.create_task(tid, topic, metadata=dict(execution_mode='offline',
        evaluation_id=directory.parent.name, evaluation_case_id=case_id))
    os.environ.update(MODEL_FAILOVER_ENABLED='false' if live else 'true',
        MODEL_MAX_RETRIES=str(EVAL_MODEL_MAX_RETRIES), MAX_TASK_TOKENS='500000',
        MODEL_COMPLETION_RESERVE=str(EVAL_COMPLETION_RESERVE),
        MODEL_TIMEOUT_SECONDS=str(EVAL_MODEL_TIMEOUT_SECONDS))
    calls = Counter()
    if not live:
        os.environ.update(OPENAI_MODEL='fixture-primary', BACKUP_OPENAI_MODEL='fixture-backup',
            BACKUP_OPENAI_BASE_URL='https://fixture.invalid/v1', BACKUP_OPENAI_API_KEY='fixture',
            MODEL_FAILOVER_TOTAL_SECONDS='30')
        reset_routing()
        if group == 'backup':
            bind_backup(ModelRoute(task_id=tid, model='fixture-backup', provider='fixture', reason='MODEL_CONNECTION', switched_at='fixture'))

    async def supplied(**kwargs):
        from services.budget import BudgetExceeded
        if calls['primary'] + calls['backup'] >= int(os.environ.get('SMARTBRIEF_LOCAL_MODEL_CALL_LIMIT', '10')):
            raise BudgetExceeded('替身供应商尝试次数上限已到')
        role = 'backup' if kwargs['model'] == 'fixture-backup' else 'primary'
        calls[role] += 1
        if os.environ.get('SMARTBRIEF_EVAL_FAULT') == 'both_unavailable' or group == 'mixed' and role == 'primary':
            import httpx
            from openai import APIConnectionError
            raise APIConnectionError(request=httpx.Request('POST', 'https://fixture.invalid'))
        agent = usage_context.get()[1]
        calls[agent] += 1
        if agent == 'planner':
            text = json.dumps([dict(id=1, agent='researcher', task=topic, depends_on=[]),
                               dict(id=2, agent='writer', task=topic, depends_on=[1])], ensure_ascii=False)
        elif agent == 'researcher':
            text = json.dumps(dict(action='web_search', query=topic) if calls[agent] == 1 else dict(action='done'), ensure_ascii=False)
        elif agent in ('writer', 'rewriter'):
            text = '## 固定资料简报\n\n' + '\n\n'.join('；'.join(f['statement'] for f in s['facts']) + f'。[S{i+1}]' for i, s in enumerate(sources))
        elif agent == 'reviewer':
            text = '{"passed":true,"comments":[]}'
        else:
            raise ValueError('未知供应商调用归属')
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))],
                               usage=dict(prompt_tokens=100, completion_tokens=100))

    def sdk(create):
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    start = perf_counter()
    with ExitStack() as stack:
        if live:
            from openai import AsyncOpenAI
            import httpx
            from evaluation.authorization import EvaluationGrant, CallBudget
            grant = EvaluationGrant.model_validate_json(os.environ['SMARTBRIEF_EVALUATION_GRANT'])
            if group != 'primary' or case_id not in grant.case_ids or grant.dataset_hash != catalog.summary()['dataset_hash']:
                raise ValueError('授权不匹配，停止子进程')
            external = AsyncOpenAI(base_url=grant.base_url, api_key=os.environ['OPENAI_API_KEY'], max_retries=0,
                                   http_client=httpx.AsyncClient(follow_redirects=False))
            create = CallBudget(grant, external.chat.completions.create)
            stack.enter_context(patch.object(client, 'get_model', lambda *args, **kwargs: grant.model))
            stack.enter_context(patch.object(client, 'get_client', lambda: sdk(create)))
        else:
            stack.enter_context(patch.object(client, 'get_client', lambda: sdk(supplied)))
            stack.enter_context(patch.object(client, 'get_backup_client', lambda: sdk(supplied)))
        frozen = dict(query=topic, source='frozen_evaluation', results=[dict(title=s['title'], url=s['url'],
            snippet='；'.join(f['statement'] for f in s['facts'])) for s in sources])
        stack.enter_context(patch.object(react_researcher.mcp_manager, 'call_tool', AsyncMock(return_value=frozen)))
        stack.enter_context(patch.object(react_researcher, '_exa_search', AsyncMock(return_value=None)))
        stack.enter_context(patch.object(long_term_memory, '_MEMORY_ENABLED', False))
        stack.enter_context(patch.object(skills_manager, 'SKILL_CONFIG_PATH', directory / 'skill_config.json'))
        stack.enter_context(patch.object(skills_manager, 'SKILLS_POOL_DIR', directory / 'skills_pool'))
        stack.enter_context(patch.object(builtin_tools, 'CONFIG_PATH', directory / 'tool_config.json'))
        for name in ('search_template_structure', 'search_rag_terms'):
            stack.enter_context(patch.object(store, name, return_value=[]))
        try:
            await run_task(tid, topic)
        except Exception:
            pass  # 仅从实际持久化状态收集失败；不把异常当完成。
        finally:
            if live:
                await external.close()
    task = task_repository.get_task(tid)
    report = task.get('final_report') or ''
    rows = usage_repository.list_usage(tid)
    known = bool(rows) and all(r['usage_known'] for r in rows)
    trace = [dict(kind='model_call', **{k: row.get(k) for k in ('model_role', 'status', 'error_code', 'duration_ms', 'created_at')}) for row in rows]
    result = FullTaskResult(case_id=case_id, check_status='pass' if task['execution_status'] == 'completed' else 'fail',
        task_execution_status=task['execution_status'], business_quality=task['quality_status'],
        report=report, report_sha256=digest(report), evidence=task['evidence'], trace=trace,
        calls=len(rows), input_tokens=sum(r['input_tokens'] for r in rows) if known else None,
        provider_calls=create.calls if live else calls['primary'] + calls['backup'],
        output_tokens=sum(r['output_tokens'] for r in rows) if known else None,
        duration_seconds=round(perf_counter()-start, 3), error=None if task['execution_status']=='completed' else '完整工作流未完成，请查看状态和调用分类')
    (directory / 'result.json').write_text(result.model_dump_json(indent=2), encoding='utf8')


if __name__ == '__main__':
    asyncio.run(execute(Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]))
