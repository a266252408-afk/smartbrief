"""独立评分真实SDK协议与归档，只替换外部供应商。"""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS
from tests.test_evaluation_live_consent import configure, consent


def wait_run(client,rid):
    import time
    for _ in range(200):
        row=client.get('/api/v2/evaluation-center/runs/'+rid).json()['data']
        if row['execution_status']!='running':
            return row
        time.sleep(.01)
    raise AssertionError('评分没有在测试期限内完成')


def install_sdk(monkeypatch, outcome='pass'):
    captured=[]
    async def create(**kwargs):
        captured.append(kwargs)
        if outcome=='broken':
            raise TimeoutError('PRIVATE_SECRET')
        payload=json.loads(kwargs['messages'][1]['content'])
        label=outcome if outcome in ('pass','fail','unknown','not_applicable') else 'pass'
        content=json.dumps(dict(label=label,reason='依据所给资料作出独立判定',evidence_refs=[payload['sources'][0]['id']]))
        if outcome=='invalid':
            content='not-json'
        elif outcome=='truncated':
            content='{"label":"pass","reason":"响应被截断'
        elif outcome=='fenced':
            content='```json\n'+content+'\n```'
        elif outcome=='prose':
            content='判定如下：\n'+content+'\n以上。'
        elif outcome=='extra':
            content=json.dumps(dict(label=label,reason='附带了多余字段',evidence_refs=[payload['sources'][0]['id']],confidence=0.9))
        elif outcome=='empty':
            content=None
        finish_reason='length' if outcome=='truncated' else ('stop' if content is not None else None)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content),finish_reason=finish_reason)],
            usage=SimpleNamespace(prompt_tokens=100,completion_tokens=20))
    def sdk(**kwargs):
        assert kwargs['max_retries']==0 and kwargs['base_url']=='https://provider.invalid/v1'
        async def close():
            await kwargs['http_client'].aclose()
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=close)
    monkeypatch.setattr('openai.AsyncOpenAI',sdk)
    return captured


def test_real_report_grading_async_ledger_and_single_use(monkeypatch):
    from main import app
    configure(monkeypatch)
    captured=install_sdk(monkeypatch)
    body=dict(target='report',case_id='G002',report='腕戴设备情况 [S01]',live_consent=consent())
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        assert client.post('/api/v2/evaluation-center/judge',headers=READ_HEADERS,json=body).status_code==403
        response=client.post('/api/v2/evaluation-center/judge',json=body)
        assert response.status_code==202
        row=wait_run(client,response.json()['data']['run_id'])
        assert row['profile']=='independent_judging' and row['execution_status']=='completed'
        assert row['results'][0]['check_status']=='pass' and row['report_quality']=='passed'
        assert row['metadata']['judge_usage']['calls']==len(captured)>0
        assert row['metadata']['judge_usage']['input_tokens']==100*len(captured)
        assert row['metadata']['judge_usage']['estimated_costs']['USD']>0
        assert row['metadata']['judge_same_as_primary']
        assert client.post('/api/v2/evaluation-center/judge',json=body).status_code==409
        assert 'secret-not-returned' not in str(row)


@pytest.mark.parametrize('outcome',['invalid','broken'])
def test_failed_judge_is_unknown_and_usage_not_fabricated(monkeypatch,outcome):
    from main import app
    configure(monkeypatch)
    install_sdk(monkeypatch,outcome)
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        response=client.post('/api/v2/evaluation-center/judge',json=dict(target='report',case_id='G002',report='说明 [S01]',live_consent=consent()))
        assert response.status_code==202
        row=wait_run(client,response.json()['data']['run_id'])
        assert row['results'][0]['check_status']=='unknown' and row['report_quality']=='unknown'
        assert 'PRIVATE_SECRET' not in str(row)
        item=row['metadata']['judge_usage']['items'][-1]
        if outcome=='broken':
            assert row['metadata']['judge_usage']['input_tokens'] is None
            assert item['diagnostic']=='TimeoutError' and item['response_excerpt'] is None
        else:
            assert row['metadata']['judge_usage']['input_tokens']>0
            assert item['diagnostic']=='JUDGE_JSON_INVALID' and item['response_excerpt']=='not-json'
            assert 'JUDGE_JSON_INVALID' in row['results'][0]['judgements'][1]['reason']


@pytest.mark.parametrize('outcome',['fenced','prose'])
def test_judge_parses_wrapped_json(monkeypatch,outcome):
    from main import app
    configure(monkeypatch)
    install_sdk(monkeypatch,outcome)
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        response=client.post('/api/v2/evaluation-center/judge',json=dict(target='report',case_id='G002',report='腕戴设备情况 [S01]',live_consent=consent()))
        row=wait_run(client,response.json()['data']['run_id'])
        assert row['results'][0]['check_status']=='pass'


@pytest.mark.parametrize('outcome,diagnostic',[
    ('extra','JUDGE_SCHEMA_INVALID'),('truncated','JUDGE_TRUNCATED'),('empty','JUDGE_EMPTY_RESPONSE')])
def test_judge_failure_diagnostics_are_specific(monkeypatch,outcome,diagnostic):
    from main import app
    configure(monkeypatch)
    install_sdk(monkeypatch,outcome)
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        response=client.post('/api/v2/evaluation-center/judge',json=dict(target='report',case_id='G002',report='腕戴设备情况 [S01]',live_consent=consent()))
        row=wait_run(client,response.json()['data']['run_id'])
        assert row['results'][0]['check_status']=='unknown'
        item=row['metadata']['judge_usage']['items'][-1]
        assert item['diagnostic']==diagnostic
        if outcome=='empty':
            assert item['response_excerpt'] is None
        else:
            assert item['response_excerpt']
        assert diagnostic in row['results'][0]['judgements'][1]['reason']
        if outcome=='truncated':
            assert item['finish_reason']=='length'


def test_judge_timeout_setting_is_shared(monkeypatch):
    import asyncio
    from evaluation.real_judge import RealJudge,reserve_grading,LiveJudgeRequest
    configure(monkeypatch)
    monkeypatch.setenv('EVALUATION_JUDGE_TIMEOUT_SECONDS','12')
    async def scenario():
        captured=install_sdk(monkeypatch)
        async def update(value):
            pass
        grant=reserve_grading(LiveJudgeRequest(target='calibration',live_consent=consent()|{'max_model_calls':1}),['J01'])
        judge=RealJudge(grant,update)
        assert judge.timeout_seconds==17
        assert (await judge(dict(query='x',sources=[dict(id='S01')])))['label']=='pass'
        assert captured[-1]['timeout']==12
        await judge.close()
    asyncio.run(scenario())


def test_real_calibration_does_not_leak_reference_labels(monkeypatch):
    from main import app
    configure(monkeypatch)
    captured=install_sdk(monkeypatch)
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        response=client.post('/api/v2/evaluation-center/judge',json=dict(target='calibration',live_consent=consent()))
        assert response.status_code==202
        row=wait_run(client,response.json()['data']['run_id'])
        assert len(row['results'])==10 and row['metadata']['calibration']['false_accepts']==5
        for request in captured:
            payload=json.loads(request['messages'][1]['content'])
            assert not {'expected','oracle','split','label'} & payload.keys()


def test_missing_consent_or_invalid_target_no_calls(monkeypatch):
    from main import app
    configure(monkeypatch)
    captured=install_sdk(monkeypatch)
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        for body in (dict(target='report',case_id='G002',report='x'),
            dict(target='report',case_id='G001',report='x',live_consent=consent()),
            dict(target='run_result',run_id='er_'+'a'*32,case_id='G017',live_consent=consent())):
            assert client.post('/api/v2/evaluation-center/judge',json=body).status_code in (404,422)
        assert captured==[]


def test_run_result_judgement_binds_original_and_does_not_rewrite(monkeypatch):
    from main import app
    from db import evaluation_run_repository as repository
    from evaluation.report_grading import digest
    configure(monkeypatch)
    install_sdk(monkeypatch)
    rid='er_'+'a'*32
    original=dict(run_id=rid,profile='full_task_live',execution_status='completed',metadata=dict(dataset_hash=consent()['dataset_hash']),
        results=[dict(case_id='G017',report='概要 [S1]',report_sha256=digest('概要 [S1]'),evidence=[dict(source_id='S1',title='资料',excerpt='概述事实',url='https://reference.invalid')])])
    repository.save(original)
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        response=client.post('/api/v2/evaluation-center/judge',json=dict(target='run_result',run_id=rid,case_id='G017',live_consent=consent()))
        assert response.status_code==202
        row=wait_run(client,response.json()['data']['run_id'])
        assert row['metadata']['source_run_id']==rid and row['metadata']['source_result_sha256']
        assert repository.get(rid)==original


def test_search_result_reassessment_and_invalid_originals(monkeypatch):
    from main import app
    from db import evaluation_run_repository as repository
    configure(monkeypatch)
    install_sdk(monkeypatch)
    rid='er_'+'b'*32
    record=dict(run_id=rid,profile='search_live',execution_status='completed',metadata=dict(dataset_hash=consent()['dataset_hash']),
        results=[dict(case_id='G001',evidence=[dict(title='当次资料',snippet='检索片段',url='https://source.invalid')])])
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        body=dict(target='run_result',run_id=rid,case_id='G001',live_consent=consent())
        for changes in ({'execution_status':'running'},{'profile':'mechanism_regression'},{'metadata':{'dataset_hash':'b'*64}}):
            repository.save(record|changes)
            assert client.post('/api/v2/evaluation-center/judge',json=body).status_code==422
        repository.save(record)
        assert client.post('/api/v2/evaluation-center/judge',json=body|{'case_id':'G003'}).status_code==422
        response=client.post('/api/v2/evaluation-center/judge',json=body)
        row=wait_run(client,response.json()['data']['run_id'])
        assert [j['rubric_id'] for j in row['results'][0]['judgements']]==['S_RELEVANCE','S_COVER','S_FRESH']


def test_judge_configuration_and_budget_reject_before_sdk(monkeypatch):
    from evaluation.real_judge import LiveJudgeRequest,reserve_grading
    from services.evaluation_service import EvaluationError
    from services.evaluation_live_config import read_config
    configure(monkeypatch)
    for changed in ({'model':'other'},{'base_url':'https://other.invalid'},{'dataset_hash':'b'*64},{'estimated_cost_ceiling':0.0001}):
        with pytest.raises(EvaluationError):
            reserve_grading(LiveJudgeRequest(target='calibration',live_consent=consent()|changed),['J01'])
    monkeypatch.setenv('EVALUATION_JUDGE_MODEL','separate-judge')
    assert not read_config()['judge']['ready']
    monkeypatch.setenv('EVALUATION_JUDGE_BASE_URL','https://provider.invalid/v1')
    monkeypatch.setenv('EVALUATION_JUDGE_API_KEY','judge-private-key')
    config=read_config()['judge']
    assert config['ready'] and not config['same_as_primary'] and 'private-key' not in str(config)


def test_judge_boundaries_and_cancellation_preserve_attempts(monkeypatch):
    import asyncio
    from evaluation.real_judge import RealJudge,reserve_grading,LiveJudgeRequest
    configure(monkeypatch)
    async def scenario():
        captured=install_sdk(monkeypatch)
        updates=[]
        async def update(value):
            updates.append(value)
        grant=reserve_grading(LiveJudgeRequest(target='calibration',live_consent=consent()|{'max_model_calls':1}),['J01'])
        judge=RealJudge(grant,update)
        payload=dict(query='任务',response='内容',criterion='依据',sources=[dict(id='S01')])
        assert (await judge(payload))['label']=='pass'
        assert (await judge(payload))['label']=='unknown'
        assert len(captured)==1 and updates[-1]['calls']==1 and updates[-1]['items'][-1]['error']=='JUDGE_BUDGET'
        await judge.close()
        async def interrupted(**kwargs):
            raise asyncio.CancelledError
        judge=RealJudge(grant,update)
        judge.budget.create=interrupted
        with pytest.raises(asyncio.CancelledError):
            await judge(payload)
        assert updates[-1]['calls']==1 and updates[-1]['items'][-1]['error']=='JUDGE_INTERRUPTED'
        await judge.close()
    asyncio.run(scenario())


def test_judge_rejects_unbound_pass_and_keeps_dict_usage(monkeypatch):
    import asyncio
    from evaluation.real_judge import RealJudge,reserve_grading,LiveJudgeRequest
    configure(monkeypatch)
    install_sdk(monkeypatch)
    async def scenario():
        async def update(value):
            pass
        grant=reserve_grading(LiveJudgeRequest(target='calibration',live_consent=consent()),['J01'])
        judge=RealJudge(grant,update)
        async def invalid(**kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"label":"pass","reason":"无引用","evidence_refs":[]}'))],usage={'prompt_tokens':20,'completion_tokens':3})
        judge.budget.create=invalid
        assert (await judge(dict(query='x',sources=[dict(id='S01')])))['label']=='unknown'
        assert judge.usage()['input_tokens']==20
        await judge.close()
    asyncio.run(scenario())


def test_judge_busy_storage_failure_and_config_error(monkeypatch):
    from main import app
    from services import evaluation_run_service as runs
    from services.evaluation_service import EvaluationError
    from evaluation.real_judge import LiveJudgeRequest
    from evaluation import catalog
    configure(monkeypatch)
    captured=install_sdk(monkeypatch)
    body=dict(target='calibration',live_consent=consent())
    with monkeypatch.context() as patch:
        patch.setattr(runs,'_handle',SimpleNamespace(done=lambda:False))
        with pytest.raises(EvaluationError,match='运行'):
            runs.start_judging(LiveJudgeRequest(**body))
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        with monkeypatch.context() as patch:
            def broken(record):
                raise OSError('PRIVATE_PATH')
            patch.setattr(runs.repository,'save',broken)
            assert client.post('/api/v2/evaluation-center/judge',json=body).status_code==503
        with monkeypatch.context() as patch:
            patch.setattr(catalog,'PACK_ROOT',catalog.PACK_ROOT/'missing')
            response=client.get('/api/v2/evaluation-center/live-config')
            assert response.status_code==503 and 'PRIVATE_PATH' not in response.text
    assert captured==[]
