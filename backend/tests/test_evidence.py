"""证据规范化、任务隔离、旧库备份恢复与真实HTTP读取。"""
import asyncio
import hashlib
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from db import database, task_repository as repo
from services.evidence import Evidence, canonical_url, normalize_evidence, validate_sources
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS


def source(task_id='t', **changes):
    return dict(source_id='S1', task_id=task_id, kind='web', title='测试资料', excerpt='测试证据片段', retrieved_at='2026-10-20T00:00:00+00:00', url='https://example.invalid/a') | changes


def research(*items, error=None):
    return {'step_1': {'observations': [{'result': {'results': list(items), **({'error':error} if error else {})}}]}}


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'data:text/html,x', '//example.invalid/a', 'file:///a', 'http://user:secret@example.invalid/a', 'https://@example.invalid/a', 'https://', 'https://example.invalid:bad/a', 'https://exam ple.invalid', 'https://example.invalid/\\evil'])
def test_unsafe_url_rejected(url):
    with pytest.raises(ValueError):
        canonical_url(url)
    with pytest.raises(ValidationError):
        Evidence(**source(url=url))


def test_url_and_model_boundaries():
    assert canonical_url('HTTPS://Example.Invalid:443#x') == 'https://example.invalid/'
    assert canonical_url('http://example.invalid:80/a?b=2&a=1#part') == 'http://example.invalid/a?b=2&a=1'
    assert canonical_url('http://[::1]:8080/a') == 'http://[::1]:8080/a'
    for changes in ({'retrieved_at':'bad'}, {'retrieved_at':'2026-10-20'}, {'title':' '}, {'excerpt':' '}, {'source_deleted':'false'}, {'source_id':'S0'}, {'kind':'document'}, {'kind':'document','document_id':'d','chunk_id':''}, {'score':float('inf')}, {'extra':1}):
        with pytest.raises(ValidationError):
            Evidence(**source(**changes))
    document = source(kind='document', document_id='d', chunk_id='c', page=1, url=None)
    assert Evidence(**document).kind == 'document'


def test_normalization_dedup_and_stable_mapping():
    item={'title':'来源A','snippet':'证据A','url':'https://EXAMPLE.invalid:443/a#first'}
    first=normalize_evidence('t',research(item, {'title':'重复','snippet':'重复片段','url':'https://example.invalid/a#second'}))
    assert len(first) == 1
    assert first[0]['source_id'] == 'S1'
    assert first[0]['excerpt'] == '证据A'
    second=normalize_evidence('t',research({'title':'来源B','snippet':'证据B','url':'https://example.invalid/b'}),first)
    assert [s['source_id'] for s in second] == ['S1','S2']
    assert normalize_evidence('t',research(item),second) == second
    assert all(s['task_id']=='t' for s in second)
    assert normalize_evidence('other',research(item))[0]['task_id']=='other'
    assert validate_sources('t',second) == second


def test_missing_invalid_and_failed_search_no_fabrication():
    good={'title':'A','snippet':'片段','url':'https://example.invalid/a'}
    assert normalize_evidence('t',research(good,error='搜索失败')) == []
    assert normalize_evidence('t',research({}, {'title':'A','snippet':'x','url':'javascript:x'}, {'title':'A','snippet':'','url':'https://example.invalid'}, None)) == []
    for bad in ({}, {'step':None}, {'step':{'observations':[None,{'result':None},{'result':{'results':None}}]}}, []):
        assert normalize_evidence('t',bad) == []
    assert normalize_evidence('',research(good)) == []
    for sources in ([source('other')], [source(),source()], [source(),source(source_id='S2')], None):
        with pytest.raises(ValueError):
            validate_sources('t',sources)


def test_repository_api_and_task_isolation(tmp_path):
    from main import app
    database.init_db(tmp_path/'evidence.db')
    repo.create_task('t','主题')
    repo.create_task('other','独立任务')
    evidence=normalize_evidence('t',research({'title':'来源','snippet':'片段','url':'https://example.invalid/a'}))
    repo.update_task('t',evidence=evidence)
    assert repo.get_task('t')['evidence']==evidence
    assert repo.get_task('other')['evidence']==[]
    with pytest.raises(ValueError):
        repo.update_task('other',evidence=evidence)
    with TestClient(app,headers=READ_HEADERS) as client:
        response=client.get('/api/v2/tasks/t/sources')
        assert response.status_code==200
        assert response.json()['data']=={'task_id':'t','sources':evidence}
        assert response.json()['meta']['request_id']
        assert client.get('/api/v2/tasks/other/sources').json()['data']['sources']==[]
        assert client.get('/api/v2/tasks/missing/sources').json()['error']['code']=='TASK_NOT_FOUND'
        assert client.get('/api/v2/tasks/t/sources',headers={'Authorization':''}).status_code==401
        assert client.delete('/api/v1/tasks/t').status_code==403
        assert client.get('/api/v1/tasks/t/status',headers=ADMIN_HEADERS).json()['evidence']==evidence
        with database.get_connection() as conn:
            conn.execute("UPDATE tasks SET evidence='[{}]' WHERE task_id='other'")
        invalid=client.get('/api/v2/tasks/other/sources')
        assert invalid.status_code==500
        assert invalid.json()['error']['code']=='SOURCES_INVALID'


def test_legacy_backup_restore_and_evidence_migration(tmp_path):
    original=tmp_path/'old.db'
    with sqlite3.connect(original) as conn:
        conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, topic TEXT, status TEXT, final_report TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('legacy','旧主题','completed','原报告[S1]')")
    backup=tmp_path/'backup.db'
    shutil.copy2(original,backup)
    restored=tmp_path/'restored.db'
    shutil.copy2(backup,restored)
    assert hashlib.sha256(original.read_bytes()).digest()==hashlib.sha256(restored.read_bytes()).digest()
    with sqlite3.connect(restored) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    database.init_db(restored)
    database.init_db(restored)
    task=repo.get_task('legacy')
    assert task['evidence']==[]
    assert task['quality_status']=='unknown'
    assert task['final_report']=='原报告[S1]'
    assert original.read_bytes()==backup.read_bytes()


def test_real_graph_collects_and_persists_sources(tmp_path, offline_services):
    from graph.workflow import run_task
    from agents import react_researcher
    from unittest.mock import AsyncMock, patch
    database.init_db(tmp_path/'graph.db')
    repo.create_task('t','主题')
    offline_services['planner'].outputs=['[{"id":1,"agent":"researcher","task":"搜","depends_on":[]},{"id":2,"agent":"writer","task":"写","depends_on":[1]}]']
    offline_services['react_researcher'].outputs=['{"action":"web_search","query":"资料"}','{"action":"done"}']
    offline_services['react_writer'].outputs=['## 正文[S1]']
    offline_services['reviewer'].outputs=['{"passed":true,"comments":[]}']
    result={'results':[{'title':'来源','snippet':'证据片段','url':'https://example.invalid/a'}]}
    with patch.object(react_researcher.mcp_manager,'call_tool',new=AsyncMock(return_value=result)):
        asyncio.run(run_task('t','主题'))
    assert repo.get_task('t')['evidence'][0]['source_id']=='S1'
    assert repo.get_task('t')['evidence'][0]['excerpt']=='证据片段'
