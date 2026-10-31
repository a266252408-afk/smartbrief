"""回放响应解析与语义未知分开，不把冻结参考当真实搜索成绩。"""
import asyncio
import time

from fastapi.testclient import TestClient
import pytest
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS


def test_replay_freshness_duplicates_and_unavailable():
    from evaluation.search_replay import run_case
    async def scenario():
        old=await run_case('C003')
        assert old['metrics']['stale_documents']==1 and old['metrics']['relevance_at_5'] is None
        unknown=await run_case('C004')
        assert unknown['metrics']['unknown_period_documents']==1
        copies=await run_case('C006')
        assert copies['metrics']['exact_duplicate_count']==4 and copies['metrics']['unique_sources']==1
        blocked=await run_case('C007')
        assert blocked['metrics']['unavailable_content_count']==1
        gold=await run_case('G001')
        assert gold['metrics']['network_calls']==0 and gold['check_status']=='unknown'
        assert gold['evidence'][0]['origin']=='frozen_reference' and gold['trace']
    asyncio.run(scenario())


def test_search_request_only_accepts_prepared_cases():
    from evaluation.search_replay import SearchRequest, canonical_url
    for body in ({'case_ids':[]},{'case_ids':['R009']},{'case_ids':['G001','G001']},{'case_ids':['G001'],'url':'file:///bad'}):
        with pytest.raises(ValueError):
            SearchRequest.model_validate(body)
    assert canonical_url('https://EXAMPLE.com/a/?utm_source=x&id=2#fragment')=='https://example.com/a?id=2'
    assert canonical_url('file:///bad') is None
    assert canonical_url('https://[broken') is None
    from evaluation.search_replay import run_case
    with pytest.raises(ValueError):
        asyncio.run(run_case('R009'))


def test_real_search_replay_http_and_live_denial():
    from main import app
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        assert client.post('/api/v2/evaluation-center/runs',headers=READ_HEADERS,json={'profile':'search_replay','case_ids':['G001']}).status_code==403
        assert client.post('/api/v2/evaluation-center/runs',json={'profile':'search_live','case_ids':['G001']}).status_code==403
        assert client.get('/api/v2/evaluation-center/runs').json()['data']['items']==[]
        response=client.post('/api/v2/evaluation-center/runs',json={'profile':'search_replay','case_ids':['G001','C003']})
        assert response.status_code==202
        rid=response.json()['data']['run_id']
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            record=client.get('/api/v2/evaluation-center/runs/'+rid).json()['data']
            if record['execution_status']!='running':
                break
            time.sleep(.03)
        assert record['execution_status']=='completed' and len(record['results'])==2
        assert record['summary']['unknown']==2 and record['summary']['passed']==0
        assert record['metadata']['model']=='none' and record['metadata']['online_validated'] is False
