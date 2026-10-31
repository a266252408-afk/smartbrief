"""人工核验与机器成绩分离，版本绑定且审计保留。"""
from fastapi.testclient import TestClient
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS
import pytest


def test_case_review_persists_without_mutating_frozen_pack():
    from main import app
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        before=client.get('/api/v2/evaluation-center/catalog').json()['data']
        body=dict(target='case_definition',case_id='G002',run_id=None,dataset_hash=before['dataset_hash'],decision='pass',notes='已核对原始资料。')
        result=client.post('/api/v2/evaluation-center/reviews',json=body)
        assert result.status_code==201
        review=result.json()['data']
        assert review['actor']=='admin' and review['decision']=='pass'
        after=client.get('/api/v2/evaluation-center/catalog').json()['data']
        assert after['dataset_hash']==before['dataset_hash']
        assert next(c for c in after['cases'] if c['case_id']=='G002')['human_review']=='pass'
        assert next(c for c in after['cases'] if c['case_id']=='G001')['human_review']=='pending'
        second=client.post('/api/v2/evaluation-center/reviews',json=body|{'decision':'unknown','notes':'发现口径需进一步核对。'}).json()['data']
        assert second['supersedes']==review['review_id']
        records=client.get('/api/v2/evaluation-center/reviews',headers=READ_HEADERS).json()['data']['items']
        assert len(records)==2
        assert client.post('/api/v2/evaluation-center/reviews',headers=READ_HEADERS,json=body).status_code==403
        assert client.post('/api/v2/evaluation-center/reviews',json=body|{'dataset_hash':'a'*64}).status_code==409
        for change in ({'case_id':'G999'},{'target':'unexpected'},{'notes':'x'*2001},{'notes':'  '},{'extra':'bad'}):
            assert client.post('/api/v2/evaluation-center/reviews',json=body|change).status_code in (404,422)


def test_run_review_requires_actual_result_and_never_changes_machine_score():
    from main import app
    from db import evaluation_run_repository as repo
    from evaluation import catalog
    data=catalog.summary()
    rid='er_'+'a'*32
    record=dict(run_id=rid,execution_status='completed',metadata=dict(dataset_hash=data['dataset_hash']),
                case_ids=['R009'],results=[dict(case_id='R009',check_status='fail',report_quality='unknown')])
    repo.save(record)
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        body=dict(target='run_result',case_id='R009',run_id=rid,dataset_hash=data['dataset_hash'],decision='unknown',notes='请复核失败原因。')
        assert client.post('/api/v2/evaluation-center/reviews',json=body).status_code==201
        assert repo.get(rid)==record
        assert client.post('/api/v2/evaluation-center/reviews',json=body|{'case_id':'G002'}).status_code==409
        assert client.post('/api/v2/evaluation-center/reviews',json=body|{'run_id':None}).status_code==422


def test_review_store_errors_never_look_like_no_reviews(monkeypatch):
    from main import app
    from services import evaluation_review_service as service
    from db import evaluation_review_repository as repo
    from evaluation import catalog
    pack=catalog.summary()
    body=service.EvaluationReviewRequest(target='case_definition',case_id='G002',dataset_hash=pack['dataset_hash'],decision='unknown',notes='核对中')
    with pytest.raises(service.EvaluationError) as exc:
        service.create(body,'readonly')
    assert exc.value.status==403
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        def broken(*args):
            raise OSError('SECRET_INTERNAL')
        monkeypatch.setattr(repo,'append',broken)
        response=client.post('/api/v2/evaluation-center/reviews',json=body.model_dump())
        assert response.status_code==503 and 'SECRET_INTERNAL' not in response.text
        monkeypatch.setattr(repo,'list_reviews',broken)
        assert client.get('/api/v2/evaluation-center/reviews').status_code==503
        assert client.get('/api/v2/evaluation-center/catalog').status_code==503


def test_review_repository_is_append_only_and_atomic(monkeypatch):
    from db import evaluation_review_repository as repo
    row=dict(review_id='evr_'+'a'*32,recorded_at='fixture')
    repo.append(row)
    with pytest.raises(FileExistsError):
        repo.append(row)
    with pytest.raises(ValueError):
        repo.append(row|{'review_id':'../bad'})
    def broken(*args):
        raise OSError('disk')
    monkeypatch.setattr(repo.os,'replace',broken)
    with pytest.raises(OSError):
        repo.append(row|{'review_id':'evr_'+'b'*32})
    assert not list(repo.root().rglob('*.tmp'))


def test_review_of_corrupt_run_fails_explicitly(monkeypatch):
    from services import evaluation_review_service as service
    from evaluation import catalog
    from db import evaluation_run_repository as repo
    pack=catalog.summary()
    body=service.EvaluationReviewRequest(target='run_result',case_id='R009',run_id='er_'+'a'*32,dataset_hash=pack['dataset_hash'],decision='unknown',notes='核对')
    def broken(*args):
        raise ValueError('SECRET')
    monkeypatch.setattr(repo,'get',broken)
    with pytest.raises(service.EvaluationError) as exc:
        service.create(body,'admin')
    assert exc.value.code=='REVIEW_STORAGE_FAILED' and 'SECRET' not in str(exc.value)
