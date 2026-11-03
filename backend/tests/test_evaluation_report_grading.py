"""独立评判与校准只替换供应商边界，不把标签喂给评分器。"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.auth_support import ADMIN_HEADERS, READ_HEADERS


def test_rules_unknown_and_no_business_reviewer():
    from evaluation.report_grading import ReportGradeRequest, grade_report
    async def scenario():
        for report in ('', '尚待补充 [S编号]', '数字 [S99]'):
            result = await grade_report(ReportGradeRequest(case_id='G002', report=report))
            assert result['check_status'] == 'fail'
        result = await grade_report(ReportGradeRequest(case_id='G002', report='材料说明 [S01]'))
        assert result['check_status'] == 'unknown' and result['report_quality'] == 'unknown'
        assert result['judgements'][1]['label'] == 'unknown'
    asyncio.run(scenario())


def test_citation_structure_separates_gap_disclosure():
    from evaluation.report_grading import ReportGradeRequest, grade_report
    async def scenario():
        gap = await grade_report(ReportGradeRequest(case_id='G002', report='2025年出货12万台[S01]。其他指标数据待补充。'))
        assert gap['judgements'][0]['label'] == 'pass'
        spaced = await grade_report(ReportGradeRequest(case_id='G002', report='2025年出货12万台[ S01 ]。'))
        assert spaced['judgements'][0]['label'] == 'pass'
        reasons = {}
        for report in ('', '说明文字', '说明[S99]', '说明[S编号]', '[S01]'):
            result = await grade_report(ReportGradeRequest(case_id='G002', report=report))
            reasons[report] = result['judgements'][0]['reason']
            assert result['judgements'][0]['label'] == 'fail'
        assert '空稿' in reasons['']
        assert '缺少' in reasons['说明文字']
        assert 'S99' in reasons['说明[S99]']
        assert '占位符' in reasons['说明[S编号]']
        assert '绑定' in reasons['[S01]']
    asyncio.run(scenario())


def test_adapter_strictness_hard_failure_and_timeout():
    from evaluation.report_grading import ReportGradeRequest, grade_report, OpenJudgeAdapter
    async def scenario():
        class Grader:
            async def aevaluate(self, **payload):
                assert 'oracle' not in payload and 'label' not in payload
                return SimpleNamespace(score=1.0, reason='给定材料充分支持')
        good = OpenJudgeAdapter(Grader())
        body = ReportGradeRequest(case_id='G002', report='材料说明 [S01]')
        assert (await grade_report(body, good))['check_status'] == 'pass'
        assert (await grade_report(body.model_copy(update={'report': '[S编号] [S01]'}), good))['check_status'] == 'fail'
        assert (await grade_report(body.model_copy(update={'report': '数据待补充 [S01]'}), good))['check_status'] == 'pass'
        for score in (True, float('nan'), 0.5, '1'):
            class Bad:
                async def aevaluate(self, **payload):
                    return SimpleNamespace(score=score, reason='无效')
            assert (await grade_report(body, OpenJudgeAdapter(Bad())))['check_status'] == 'unknown'
        class Broken:
            async def __call__(self, payload):
                raise TimeoutError('密钥不能出现在结果')
        result = await grade_report(body, Broken())
        assert result['check_status'] == 'unknown' and '密钥' not in str(result)
    asyncio.run(scenario())


def test_calibration_label_isolation_and_unknown_denominator():
    from evaluation.report_grading import calibrate
    async def scenario():
        calls = []
        async def grader(payload):
            calls.append(payload)
            assert not {'label', 'expected', 'split', 'id'} & payload.keys()
            return {'label': 'pass', 'reason': '替身统一放行，仅测误放行计数'}
        result = await calibrate(grader)
        assert len(calls) == 10 and result['total'] == 10
        assert result['false_accepts'] == 5 and result['false_rejects'] == 0
        assert result['groups']['evaluation_only']['total'] == 3
        missing = await calibrate()
        assert missing['unknown'] == 10 and missing['accuracy'] is None
    asyncio.run(scenario())


def test_report_grading_http_archive_and_permissions():
    from main import app
    with TestClient(app, headers=ADMIN_HEADERS) as client:
        path = '/api/v2/evaluation-center/grade-report'
        body = dict(profile='report_grading', case_id='G002', report='材料说明 [S01]')
        assert client.post(path, headers=READ_HEADERS, json=body).status_code == 403
        assert client.post(path, json=body | {'case_id': 'G001'}).status_code == 422
        records = [client.post(path, json=body).json()['data'] for _ in range(2)]
        assert records[0]['run_id'] != records[1]['run_id']
        read = client.get('/api/v2/evaluation-center/runs/' + records[0]['run_id']).json()['data']
        assert read['results'][0]['check_status'] == 'unknown'
        assert read['metadata']['grader_version'] and read['metadata']['rubric_hash']
        calibrated = client.post('/api/v2/evaluation-center/calibrate').json()['data']
        assert calibrated['unknown'] == 10 and calibrated['accuracy'] is None
        run = client.get('/api/v2/evaluation-center/runs/' + calibrated['run_id']).json()['data']
        assert len(run['results']) == 10 and run['profile'] == 'grader_calibration'
        response = client.post('/api/v2/evaluation-center/reviews', json=dict(target='run_result',
            case_id='J01', run_id=run['run_id'], dataset_hash=run['metadata']['dataset_hash'], decision='unknown', notes='仅测试校准复核归属'))
        assert response.status_code == 201
        assert client.post('/api/v2/evaluation-center/reviews', json=dict(target='case_definition',
            case_id='J01', dataset_hash=run['metadata']['dataset_hash'], decision='pass', notes='不能改校准真值')).status_code == 422


def test_report_request_rejects_unknown_fields():
    from evaluation.report_grading import ReportGradeRequest
    with pytest.raises(ValueError):
        ReportGradeRequest.model_validate(dict(case_id='G002', report='x', oracle='注入答案'))


def test_grader_cannot_reference_other_source():
    from evaluation.report_grading import ReportGradeRequest, grade_report
    async def bad(payload):
        return dict(label='pass', reason='引用越界资料', evidence_refs=['S999'])
    result = asyncio.run(grade_report(ReportGradeRequest(case_id='G002', report='材料说明 [S01]'), bad))
    assert result['check_status'] == 'unknown'


def test_grading_storage_and_catalog_errors(monkeypatch):
    from main import app
    from db import evaluation_run_repository
    from evaluation import catalog
    with TestClient(app, headers=ADMIN_HEADERS) as client:
        with monkeypatch.context() as patch:
            def broken(record):
                raise OSError('私人路径不可暴露')
            patch.setattr(evaluation_run_repository, 'save', broken)
            response = client.post('/api/v2/evaluation-center/grade-report', json=dict(case_id='G002', report='说明 [S01]'))
            assert response.status_code == 503 and '私人路径' not in response.text
            assert client.post('/api/v2/evaluation-center/calibrate').status_code == 503
        monkeypatch.setattr(catalog, 'PACK_ROOT', catalog.PACK_ROOT / 'missing')
        assert client.post('/api/v2/evaluation-center/calibrate').status_code == 503
        assert client.post('/api/v2/evaluation-center/grade-report', json=dict(case_id='G002', report='x')).status_code == 503
