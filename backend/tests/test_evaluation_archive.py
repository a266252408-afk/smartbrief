"""20例真实离线报告、部分失败归档与旧库迁移。"""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from db import database, evaluation_repository as repo
from evaluation import archive
from evaluation.suite import load_suite


def test_real_twenty_case_offline_report(tmp_path, monkeypatch):
    database.init_db(tmp_path/'evaluation.db')
    from db import memory_repository
    from services.memory_policy import MemoryEntry
    original = memory_repository.reserve(MemoryEntry(memory_id='user-memory',task_id='user-task',topic='用户原有记忆',quality_status='unknown',verified=False,status='unverified',report_hash=None,model_name=None,prompt_version=None,created_at=None))
    monkeypatch.setattr('llm.client.get_client',lambda: pytest.fail('离线协议不允许真实SDK'))
    record = asyncio.run(archive.run_offline())
    assert record['status'] == 'completed' and record['completed_cases'] == record['total_cases'] == 20
    assert len({r['case_id'] for r in record['results']}) == 20
    assert record['metadata']['mode'] == 'offline'
    assert record['metadata']['model'] == 'synthetic-protocol-1'
    assert record['metadata']['dataset_hash'] == load_suite()[1]
    assert record['metadata']['code_commit'] and record['metadata']['prompt_version']
    assert record['metadata']['online_experiment'] == dict(status='pending',results=None)
    quality = record['metrics']['quality']
    assert quality['quality_pass_rate'] == 0.75
    assert quality['pending_cases'] == 0 and quality['input_tokens'] is None
    assert quality['citation_valid_rate'] == 1.0 and quality['estimated_costs'] == {}
    assert quality['failure_categories']['BUDGET_EXCEEDED'] == 1
    assert quality['failure_categories']['TASK_CANCELLED'] == 1
    assert quality['failure_categories']['MODEL_TIMEOUT'] == 1
    assert quality['manual_fact_check']['accuracy'] is None
    assert '合成模型' in record['report_md'] and '在线实验：pending' in record['report_md']
    assert repo.get(record['eval_id']) == record
    assert memory_repository.entries() == [original]  # 不读写用户记忆或混入合成登记。
    if os.getenv('SMARTBRIEF_EVAL_ARTIFACT'):
        # 仅显式要求时交付当前真实执行结果，普通回归不改历史凭据。
        path = Path(os.environ['SMARTBRIEF_EVAL_ARTIFACT'])
        path.with_suffix('.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n','utf-8')
        path.with_suffix('.md').write_text(record['report_md'],'utf-8')


def test_partial_progress_survives_failure_and_metadata_immutable(tmp_path,monkeypatch):
    database.init_db(tmp_path/'partial.db')
    cases,digest = load_suite()
    flags = dict(rag=True,memory=True,review=True)
    repo.create('partial',20,metadata=archive.provenance(digest,flags))
    original = repo.get('partial')['metadata']
    real = archive.execute_case
    calls = 0
    async def fail_second(dependencies):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('不应泄露的内部错误')
        return await real(dependencies)
    monkeypatch.setattr(archive,'execute_case',fail_second)
    with pytest.raises(RuntimeError):
        asyncio.run(archive.execute_suite('partial',cases,flags))
    record = repo.get('partial')
    assert record['status'] == 'failed' and len(record['results']) == record['completed_cases'] == 1
    assert record['metrics']['quality']['quality_pass_rate'] == 0.05
    assert record['metadata'] == original and '内部错误' not in record['error']
    repo._update('partial',metadata={'fake':True})
    assert repo.get('partial')['metadata'] == original
    with database.get_connection() as conn:
        conn.execute("UPDATE evaluation_reports SET results='corrupt' WHERE eval_id='partial'")
    with pytest.raises(ValueError):
        repo.get('partial')


def test_old_metadata_unknown_and_migration_repeated(tmp_path):
    database.init_db(tmp_path/'old.db')
    repo.create('old',1)
    with database.get_connection() as conn:
        conn.execute('ALTER TABLE evaluation_reports DROP COLUMN metadata')
    database.init_db()
    database.init_db()
    assert repo.get('old')['metadata'] is None
    assert repo.list_reports()[0][0]['metadata'] is None
    with patch.object(archive.subprocess,'check_output',side_effect=OSError):
        result = archive.provenance('dataset',dict(rag=True,memory=True,review=True))
    assert result['code_commit'] is None and result['code_tag'] is None and result['code_dirty'] is None


def test_aggregate_rejects_invalid_denominator():
    from evaluation.quality import aggregate
    for total in (0,-1,True,1.5):
        with pytest.raises(ValueError):
            aggregate([],total)
    with pytest.raises(ValueError):
        aggregate([{},{}],1)
