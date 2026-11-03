"""真实入口授权只替换外部边界，不实际联网。"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS


def configure(monkeypatch):
    monkeypatch.setenv('OPENAI_MODEL', 'configured-model')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://provider.invalid/v1')
    monkeypatch.setenv('OPENAI_API_KEY', 'secret-not-returned')
    monkeypatch.setenv('EXA_API_KEY', 'search-secret')


def consent():
    from evaluation import catalog
    return dict(confirmed=True, authorization_id='ui-single-consumption', dataset_hash=catalog.summary()['dataset_hash'],
        model='configured-model', base_url='https://provider.invalid/v1', max_model_calls=10,
        max_input_bytes=20000, max_output_tokens=2000, input_price_per_million=1.0,
        output_price_per_million=2.0, currency='USD', estimated_cost_ceiling=1.0)


def test_live_config_readonly_and_no_secrets(monkeypatch):
    from main import app
    configure(monkeypatch)
    with TestClient(app, headers=READ_HEADERS) as client:
        response=client.get('/api/v2/evaluation-center/live-config')
        assert response.status_code==200
        data=response.json()['data']
        assert data['model']['ready'] and data['search']['ready']
        assert data['model']['name']=='configured-model'
        assert 'secret' not in response.text


def test_explicit_consent_is_single_use_and_configuration_bound(monkeypatch):
    from evaluation.full_task import FullTaskRequest
    from evaluation.authorization import reserve
    from services.evaluation_service import EvaluationError
    configure(monkeypatch)
    body=FullTaskRequest(profile='full_task_live',case_ids=['G017'],live_consent=consent())
    grant=reserve(body)
    assert grant.model=='configured-model' and grant.case_ids==['G017']
    assert datetime.fromisoformat(grant.expires_at)>datetime.now(timezone.utc)
    with pytest.raises(EvaluationError):
        reserve(body)
    monkeypatch.setenv('OPENAI_MODEL','changed-model')
    with pytest.raises(EvaluationError):
        reserve(body)


@pytest.mark.parametrize('change',[{'confirmed':False},{'dataset_hash':'b'*64},{'model':'changed'},
    {'base_url':'https://other.invalid'},{'estimated_cost_ceiling':0.001}])
def test_invalid_consent_never_creates_reservation(monkeypatch,change):
    from evaluation.full_task import FullTaskRequest
    from evaluation.authorization import reserve
    from db import evaluation_run_repository
    configure(monkeypatch)
    with pytest.raises((ValueError,RuntimeError)):
        reserve(FullTaskRequest(profile='full_task_live',case_ids=['G017'],live_consent=consent()|change))
    assert not list((evaluation_run_repository.root()/'authorizations').glob('*.json'))


def test_full_task_grant_ttl_covers_multi_case_run(monkeypatch):
    from evaluation.full_task import FullTaskRequest
    from evaluation.authorization import reserve
    configure(monkeypatch)
    body = FullTaskRequest(profile='full_task_live', case_ids=['G017', 'G018', 'G019', 'G020'],
        live_consent=consent() | {'authorization_id': 'ui-full-task-ttl', 'max_model_calls': 40,
                                  'max_input_bytes': 60000, 'estimated_cost_ceiling': 20.0})
    grant = reserve(body)
    remaining = (datetime.fromisoformat(grant.expires_at) - datetime.now(timezone.utc)).total_seconds()
    assert remaining > 100 * 60


def test_local_and_ambiguous_consent_rejected():
    from evaluation.full_task import FullTaskRequest
    from evaluation.search_replay import SearchRequest
    for model,body in [(FullTaskRequest,dict(case_ids=['G017'],live_consent=consent())),
        (FullTaskRequest,dict(profile='full_task_live',case_ids=['G017'],authorization_id='old-grant',live_consent=consent())),
        (SearchRequest,dict(case_ids=['G001'],live_consent=dict(confirmed=True)))]:
        with pytest.raises(ValueError):
            model.model_validate(body)


def test_search_consent_fixed_endpoint_and_single_use(monkeypatch):
    from evaluation.authorization import reserve_search
    from evaluation.search_replay import SearchRequest
    from services.evaluation_service import EvaluationError
    configure(monkeypatch)
    raw=dict(confirmed=True,authorization_id='search-ui-once',dataset_hash=consent()['dataset_hash'],
        per_call_estimate_ceiling=0.01,estimated_cost_ceiling=0.1)
    body=SearchRequest(profile='search_live',case_ids=['G001'],live_consent=raw)
    grant=reserve_search(body)
    assert grant.max_search_calls==1 and grant.recipient=='https://api.exa.ai/search'
    with pytest.raises(EvaluationError):
        reserve_search(body)


def test_live_config_rejects_bad_endpoint_without_exposing_query(monkeypatch):
    from services.evaluation_live_config import read_config
    configure(monkeypatch)
    monkeypatch.setenv('OPENAI_BASE_URL','https://provider.invalid/v1?key=private-value')
    data=read_config()
    assert not data['model']['ready'] and 'private-value' not in str(data)


def test_http_consent_permissions_and_archive(monkeypatch):
    from main import app
    from services import evaluation_run_service
    from evaluation.full_task import FullTaskResult
    configure(monkeypatch)
    async def external(cid,group,directory,grant):
        assert grant.model=='configured-model'
        return FullTaskResult(case_id=cid,check_status='pass',task_execution_status='completed',business_quality='passed',
            report='真实边界替身测试',report_sha256='a'*64,evidence=[],trace=[],calls=1,provider_calls=1,
            input_tokens=2,output_tokens=3,duration_seconds=0.1,error=None).model_dump()
    monkeypatch.setattr(evaluation_run_service,'full_case',external)
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        body=dict(profile='full_task_live',case_ids=['G017'],live_consent=consent())
        assert client.post('/api/v2/evaluation-center/runs',headers=READ_HEADERS,json=body).status_code==403
        response=client.post('/api/v2/evaluation-center/runs',json=body)
        assert response.status_code==202
        import time
        for _ in range(100):
            row=client.get('/api/v2/evaluation-center/runs/'+response.json()['data']['run_id']).json()['data']
            if row['execution_status']!='running':
                break
            time.sleep(.01)
        assert row['metadata']['mode']=='online' and row['summary']['passed']==1
        assert row['report_quality']=='unknown' and 'secret-not-returned' not in str(row)


def test_invalid_url_and_missing_config(monkeypatch):
    from services.evaluation_live_config import read_config
    configure(monkeypatch)
    monkeypatch.setenv('OPENAI_BASE_URL','https://[broken')
    data=read_config()
    assert not data['model']['ready']
    monkeypatch.delenv('OPENAI_API_KEY')
    monkeypatch.delenv('EXA_API_KEY')
    assert not read_config()['search']['ready']
