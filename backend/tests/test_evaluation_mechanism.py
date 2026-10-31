"""机制回归必须读取实际JUnit，不把题目预期当成绩。"""
import asyncio
from pathlib import Path

import pytest


def test_junit_does_not_count_skipped_or_missing_as_pass(tmp_path):
    from evaluation.mechanism import parse_junit
    p=tmp_path/'junit.xml'
    p.write_text('<testsuites><testsuite><testcase name="ok"/><testcase name="bad"><failure/></testcase><testcase name="skip"><skipped/></testcase></testsuite></testsuites>')
    result=parse_junit(p,1)
    assert result['test_count']==3 and result['passed']==1 and result['failed']==1 and result['skipped']==1
    assert result['check_status']=='fail'
    p.write_text('<testsuites><testsuite><testcase name="skip"><skipped/></testcase></testsuite></testsuites>')
    assert parse_junit(p,0)['check_status']=='unknown'
    p.write_text('<testsuites/>')
    assert parse_junit(p,0)['check_status']=='unknown'
    p.write_text('broken')
    assert parse_junit(p,0)['check_status']=='unknown'
    assert parse_junit(tmp_path/'missing',2)['check_status']=='unknown'


def test_run_request_rejects_arbitrary_commands_and_duplicates():
    from evaluation.mechanism import MechanismRequest
    for body in ({'case_ids':['../bad']},{'case_ids':['R009','R009']},{'case_ids':[]},{'case_ids':['R009'],'command':'echo secret'}):
        with pytest.raises(ValueError):
            MechanismRequest.model_validate(body)


def test_archive_reopen_and_path_rejection(tmp_path,monkeypatch):
    from db import database
    from db import evaluation_run_repository as repo
    monkeypatch.setattr(database,'DB_PATH',tmp_path/'data.db')
    rid='er_'+'a'*32
    repo.save(dict(run_id=rid,execution_status='running',results=[]))
    assert repo.get(rid)['results']==[] and len(repo.list_runs())==1
    with pytest.raises(ValueError):
        repo.get('../../secret')


def test_real_mechanism_subprocess_is_isolated(tmp_path):
    from evaluation.mechanism import run_case
    result=asyncio.run(run_case('R009',tmp_path,timeout=60))
    assert result['check_status']=='pass' and result['test_count']==1
    assert result['report_quality']=='unknown'
    assert (tmp_path/'junit.xml').exists()
    assert list(tmp_path.rglob('*.db'))


def test_unmapped_case_not_executed(tmp_path):
    from evaluation.mechanism import run_case
    with pytest.raises(ValueError):
        asyncio.run(run_case('C026',tmp_path))
    assert not list(Path(tmp_path).iterdir())
