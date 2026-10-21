"""合成故障真实穿过任务服务、图、预算、审核和SQLite。"""
import asyncio
import json
from collections import Counter

import pytest
from db import database, task_repository as tasks
from evaluation.suite import DATASET, load_suite
from evaluation.offline import OfflineDependencies, execute_case
from evaluation.context import evaluation_context


def test_fixed_dataset_and_reproducible_hash(tmp_path):
    cases, digest = load_suite()
    assert Counter(c['category'] for c in cases) == dict(regular=10, uncovered=3, evidence=3, fault=4)
    assert len(digest) == 64 and load_suite()[1] == digest
    with pytest.raises(ValueError):
        load_suite('other')
    raw = json.loads(DATASET.read_text('utf-8'))
    for change in ('duplicate', 'count', 'category', 'field', 'version'):
        copy = json.loads(json.dumps(raw))
        if change == 'duplicate':
            copy['cases'][1]['case_id'] = 'C01'
        elif change == 'count':
            copy['cases'].pop()
        elif change == 'category':
            copy['cases'][0]['category'] = 'fault'
        elif change == 'field':
            copy['cases'][0]['secret'] = '不允许'
        else:
            copy['suite_version'] = 'other'
        path = tmp_path / 'bad.json'
        path.write_text(json.dumps(copy), 'utf-8')
        with pytest.raises(ValueError):
            load_suite(path=path)


@pytest.mark.parametrize('index,state,quality', [(0,'completed','passed'), (10,'completed','passed'), (11,'completed','passed'), (12,'completed','passed'), (13,'completed','rejected'), (14,'completed','passed'), (15,'completed','passed'), (16,'failed','not_run'), (17,'completed','unavailable'), (18,'failed','not_run'), (19,'cancelled','not_run')])
def test_real_fault_matrix(tmp_path, monkeypatch, index, state, quality):
    database.init_db(tmp_path / 'matrix.db')
    # 任何错误地回落真实供应商的路径都立即失败。
    monkeypatch.setattr('llm.client.get_client', lambda: pytest.fail('离线协议不得创建真实SDK'))
    async def scenario():
        case = load_suite()[0][index]
        dependencies = OfflineDependencies(case, tmp_path / 'isolated')
        task_id = await execute_case(dependencies)
        assert evaluation_context.get() is None
        task = tasks.get_task(task_id)
        assert task['execution_status'] == state
        assert task['quality_status'] == quality
        if index == 13:
            assert task['forced_output'] and not task['passed']
        if index == 14:
            assert len(task['evidence']) == 2 and '[S2]' in task['final_report']
        if index == 15:
            assert len(task['evidence']) == 1 and '资料不完整' in task['final_report']
        if index == 18:
            assert task['error_code'] == 'BUDGET_EXCEEDED' and not dependencies.calls
        if index == 19:
            assert not dependencies.calls['reviewer']
        return task
    asyncio.run(scenario())


def test_dependency_context_concurrent_and_cleanup(tmp_path):
    database.init_db(tmp_path / 'contexts.db')
    async def scenario():
        cases = load_suite()[0]
        one = OfflineDependencies(cases[0], tmp_path / 'one')
        two = OfflineDependencies(cases[17], tmp_path / 'two')
        ids = await asyncio.gather(execute_case(one), execute_case(two))
        assert tasks.get_task(ids[0])['quality_status'] == 'passed'
        assert tasks.get_task(ids[1])['quality_status'] == 'unavailable'
        assert evaluation_context.get() is None
        from llm.client import get_model
        assert get_model() != one.model
        assert one.stores['rag_collection'] is not two.stores['rag_collection']
    asyncio.run(scenario())


def test_context_defaults_and_unknown_agent(tmp_path):
    from evaluation.context import enabled
    from services.usage_service import usage_context
    assert enabled('rag')
    dependencies = OfflineDependencies(load_suite()[0][0], tmp_path, dict(rag=False, memory=True, review=True))
    token = dependencies.activate()
    agent = usage_context.set(('task_fixture', 'unsupported'))
    try:
        assert not enabled('rag') and enabled('memory')
        with pytest.raises(AssertionError, match='不支持'):
            asyncio.run(dependencies.create())
    finally:
        usage_context.reset(agent)
        evaluation_context.reset(token)
