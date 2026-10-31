"""题库真实HTTP入口和只读文件完整性，不运行模型。"""
import json
import shutil

import pytest
from fastapi.testclient import TestClient
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS


def test_catalog_http_counts_and_detail():
    from main import app
    with TestClient(app,headers=READ_HEADERS) as client:
        response=client.get('/api/v2/evaluation-center/catalog')
        assert response.status_code==200
        data=response.json()['data']
        assert data['total_cases']==108 and len(data['cases'])==108
        assert data['collections']=={'golden':24,'regression':24,'challenge':40,'legacy':20}
        assert len(data['areas'])==5
        assert [a['area'] for a in data['areas'] if a['runnable']]==['protocol','search','report','robustness']
        assert all(a['score'] is None for a in data['areas'])
        row=client.get('/api/v2/evaluation-center/cases/G002').json()['data']
        assert row['case']['oracle']['required_fact_ids']==['F01','F02','F03']
        assert row['sources'][0]['publisher']=='IDC' and row['fixture'] is None
        assert row['rubrics']
        row=client.get('/api/v2/evaluation-center/cases/R009').json()['data']
        assert row['fixture']['synthetic'] and row['sources']==[]
        legacy=client.get('/api/v2/evaluation-center/cases/P-C01').json()['data']
        assert legacy['case']['case_id']=='P-C01' and legacy['case']['collection']=='legacy'
        assert client.get('/api/v2/evaluation-center/cases/G999').status_code==404
        assert client.get('/api/v2/evaluation-center/catalog?version=bad').status_code==422
        assert client.get('/api/v2/evaluation-center/catalog',headers={'Authorization':''}).status_code==401
        assert client.post('/api/v2/evaluation-center/catalog',headers=READ_HEADERS).status_code==403


@pytest.mark.parametrize('damage',['missing','tamper','path','json','duplicate','source','version','collection','fact','rubric','fixture','legacy'])
def test_catalog_rejects_corrupt_pack(tmp_path,monkeypatch,damage):
    from evaluation import catalog
    from main import app
    target=tmp_path/'pack'
    shutil.copytree(catalog.PACK_ROOT,target)
    monkeypatch.setattr(catalog,'PACK_ROOT',target)
    manifest=json.loads((target/'manifest.json').read_text('utf-8'))
    if damage=='missing':
        (target/'sources/S01.json').unlink()
    elif damage=='tamper':
        (target/'sources/S01.json').write_text('SECRET_INTERNAL',encoding='utf-8')
    elif damage=='path':
        manifest['files']['../SECRET_INTERNAL']='a'*64
    elif damage=='version':
        manifest['version']='unsupported'
    else:
        path=target/'private/golden.json'
        data=json.loads(path.read_text('utf-8'))
        if damage=='json':
            path.write_text('not json SECRET_INTERNAL',encoding='utf-8')
        else:
            if damage=='duplicate':
                data['cases'][1]['case_id']=data['cases'][0]['case_id']
            elif damage=='source':
                data['cases'][0]['input']['source_ids']=['missing']
            elif damage=='collection':
                data['cases'][0]['collection']='challenge'
            elif damage=='fact':
                data['cases'][0]['oracle']['required_fact_ids']=['missing']
            elif damage=='rubric':
                data['cases'][0]['oracle']['assertions'][0]['rubric_id']='missing'
            elif damage=='fixture':
                data['cases'][0]['input']['fixture_id']='missing'
            elif damage=='legacy':
                path=target/'legacy/protocol-1.json'
                data=json.loads(path.read_text('utf-8'))
                data['cases'][0]['topic']='changed'
            path.write_text(json.dumps(data),encoding='utf-8')
        import hashlib
        manifest['files'][path.relative_to(target).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    (target/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        response=client.get('/api/v2/evaluation-center/catalog')
        assert response.status_code==503 and response.json()['error']['code']=='CATALOG_UNAVAILABLE'
        assert 'SECRET_INTERNAL' not in response.text


def test_case_lookup_rejects_non_identifier():
    from evaluation import catalog
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.detail('../sources/S01.json')
    assert exc.value.status==404
