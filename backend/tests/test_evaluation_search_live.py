"""真实搜索只替换HTTP供应商边界，授权/解析/归档均真实运行。"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import time

from fastapi.testclient import TestClient
import pytest
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS


def approval():
    from evaluation import catalog
    return dict(authorization_id='search-fixture-approval', dataset_hash=catalog.summary()['dataset_hash'], case_ids=['G001'],
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), max_search_calls=1,
        per_call_estimate_ceiling=0.1, estimated_cost_ceiling=0.1)


def configured(monkeypatch):
    value = approval()
    monkeypatch.setenv('EXA_API_KEY', 'fixture-not-real')
    monkeypatch.setenv('SMARTBRIEF_SEARCH_EVALUATION_GRANT', json.dumps(value))
    return value


def test_search_grant_single_use_and_budget(monkeypatch):
    from evaluation.authorization import reserve_search
    from evaluation.search_replay import SearchRequest
    from services.evaluation_service import EvaluationError
    body = SearchRequest(profile='search_live', case_ids=['G001'], authorization_id='search-fixture-approval')
    monkeypatch.delenv('SMARTBRIEF_SEARCH_EVALUATION_GRANT', raising=False)
    with pytest.raises(EvaluationError):
        reserve_search(body)
    good = configured(monkeypatch)
    for change in ({'case_ids': ['G003']}, {'dataset_hash': 'a'*64}, {'expires_at': '2020-01-01T00:00:00Z'},
                   {'estimated_cost_ceiling': 0.01}, {'recipient': 'https://elsewhere.invalid'}, {'authorization_id': 'other-approval'}):
        monkeypatch.setenv('SMARTBRIEF_SEARCH_EVALUATION_GRANT', json.dumps(good | change))
        with pytest.raises(EvaluationError):
            reserve_search(body)
    monkeypatch.setenv('SMARTBRIEF_SEARCH_EVALUATION_GRANT', json.dumps(good))
    assert reserve_search(body).case_ids == ['G001']
    with pytest.raises(EvaluationError, match='已预留'):
        reserve_search(body)
    with pytest.raises(ValueError):
        SearchRequest(profile='search_replay', case_ids=['G001'], authorization_id=good['authorization_id'])


def test_live_search_http_archive_no_oracle_or_secret(monkeypatch):
    from main import app
    from evaluation import search_live
    good = configured(monkeypatch)
    calls = []
    async def provider(payload, key):
        calls.append(payload)
        assert key == 'fixture-not-real' and set(payload) == {'query', 'numResults', 'type', 'contents'}
        assert 'oracle' not in str(payload) and '3708' not in payload['query']
        return dict(results=[dict(title='当次摘要', url='https://example.invalid/a?utm_source=x', highlights=['供应商摘要'], publishedDate='2026-10-01'),
                             dict(title='重复来源', url='https://example.invalid/a', highlights=[])], costDollars=dict(total=0.0))
    monkeypatch.setattr(search_live, 'post_search', provider)
    with TestClient(app, headers=ADMIN_HEADERS) as client:
        path = '/api/v2/evaluation-center/runs'
        body = dict(profile='search_live', case_ids=['G001'], authorization_id=good['authorization_id'])
        assert client.post(path, headers=READ_HEADERS, json=body).status_code == 403
        response = client.post(path, json=body)
        assert response.status_code == 202
        rid = response.json()['data']['run_id']
        for _ in range(200):
            record = client.get(path+'/'+rid).json()['data']
            if record['execution_status'] != 'running':
                break
            time.sleep(.01)
        row = record['results'][0]
        assert row['check_status'] == 'unknown' and row['metrics']['network_calls'] == 1
        assert row['metrics']['exact_duplicate_count'] == 1 and row['metrics']['unknown_period_documents'] == 2
        assert row['metrics']['provider_estimated_cost_usd'] == 0 and row['metrics']['stale_documents'] is None
        assert record['metadata']['mode'] == 'online' and 'fixture-not-real' not in json.dumps(record)
        assert client.post(path, json=body).status_code == 409 and len(calls) == 1


def test_live_failure_expiry_and_empty_are_distinct(monkeypatch):
    from evaluation import search_live
    from evaluation.authorization import SearchEvaluationGrant
    grant = SearchEvaluationGrant(**approval())
    monkeypatch.setenv('EXA_API_KEY', 'fixture-not-real')
    async def broken(*args):
        raise TimeoutError('SECRET-CREDENTIAL')
    monkeypatch.setattr(search_live, 'post_search', broken)
    row = asyncio.run(search_live.run_case('G001', grant))
    assert row['metrics']['network_calls'] == 1 and row['metrics']['returned_count'] is None
    assert row['execution_status'] == 'failed' and 'SECRET' not in str(row)
    expired = grant.model_copy(update={'expires_at': '2020-01-01T00:00:00Z'})
    row = asyncio.run(search_live.run_case('G001', expired))
    assert row['metrics']['network_calls'] == 0
    async def empty(*args):
        return {'results': []}
    monkeypatch.setattr(search_live, 'post_search', empty)
    row = asyncio.run(search_live.run_case('G001', grant))
    assert row['execution_status'] == 'completed' and row['metrics']['returned_count'] == 0
    assert row['metrics']['provider_estimated_cost_usd'] is None and row['check_status'] == 'unknown'


def test_http_adapter_fixes_destination_and_disables_redirects(monkeypatch):
    from services import exa_evaluation_adapter as adapter
    from types import SimpleNamespace
    captured = {}
    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def post(self, endpoint, **kwargs):
            captured.update(endpoint=endpoint, **kwargs)
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'results': []})
    monkeypatch.setattr(adapter.httpx, 'AsyncClient', Client)
    assert asyncio.run(adapter.post_search({'query': '公开查询'}, 'fixture')) == {'results': []}
    assert captured['endpoint'] == 'https://api.exa.ai/search' and captured['follow_redirects'] is False
    assert captured['headers'] == {'x-api-key': 'fixture'} and captured['timeout'] == 20


@pytest.mark.parametrize('raw', [{'results': {}}, {'results': [None]}, None])
def test_bad_provider_shape_is_unknown_not_zero(monkeypatch, raw):
    from evaluation import search_live
    from evaluation.authorization import SearchEvaluationGrant
    monkeypatch.setenv('EXA_API_KEY', 'fixture-not-real')
    async def provider(*args):
        return raw
    monkeypatch.setattr(search_live, 'post_search', provider)
    row = asyncio.run(search_live.run_case('G001', SearchEvaluationGrant(**approval())))
    assert row['execution_status'] == 'failed' and row['metrics']['returned_count'] is None


def test_source_redaction_and_missing_key_never_calls(monkeypatch):
    from evaluation import search_live
    from evaluation.authorization import SearchEvaluationGrant
    grant = SearchEvaluationGrant(**approval())
    calls = []
    async def provider(*args):
        calls.append(1)
        return dict(results=[dict(title='fixture-not-real', url='https://example.invalid/fixture-not-real', text='fixture-not-real', publishedDate='bad')], costDollars='not a cost')
    monkeypatch.setenv('EXA_API_KEY', 'fixture-not-real')
    monkeypatch.setattr(search_live, 'post_search', provider)
    row = asyncio.run(search_live.run_case('G001', grant))
    assert 'fixture-not-real' not in str(row) and row['evidence'][0]['url'] is None
    assert row['metrics']['dated_documents'] == 0 and row['metrics']['provider_estimated_cost_usd'] is None
    monkeypatch.setenv('EXA_API_KEY', '')
    row = asyncio.run(search_live.run_case('G001', grant))
    assert row['metrics']['network_calls'] == 0 and len(calls) == 1
    with pytest.raises(ValueError):
        asyncio.run(search_live.run_case('G003', grant))


def test_search_total_call_limit_and_configuration_key(monkeypatch):
    from evaluation.authorization import reserve_search
    from evaluation.search_replay import SearchRequest
    from services.evaluation_service import EvaluationError
    good = configured(monkeypatch) | dict(case_ids=['G001', 'G003'])
    monkeypatch.setenv('SMARTBRIEF_SEARCH_EVALUATION_GRANT', json.dumps(good))
    body = SearchRequest(profile='search_live', case_ids=good['case_ids'], authorization_id=good['authorization_id'])
    with pytest.raises(EvaluationError):
        reserve_search(body)
    monkeypatch.setenv('EXA_API_KEY', '')
    with pytest.raises(EvaluationError):
        reserve_search(body)
