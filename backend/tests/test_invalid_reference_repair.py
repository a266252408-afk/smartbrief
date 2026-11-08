"""无效来源必须生成可执行问题，而非首轮直接终止。"""
import asyncio
from unittest.mock import AsyncMock, patch
from agents import reviewer
from services.findings import reference_findings
from services.citations import check_citations
from tests.test_evidence import source
from tests.test_rewriter import FakeResp

def test_unknown_reference_has_original_location_and_report_evidence():
    body = '# 标题\n\n声明[S999]'
    findings = reference_findings(body, check_citations('t',body,[source()]))
    assert len(findings) == 1
    assert findings[0]['location'] == {'paragraph':2,'sentence':1}
    assert findings[0]['evidence'] == {'kind':'report','quote':'声明[S999]'}
    assert 'S999' in findings[0]['problem']

def test_unknown_reference_routes_to_rewrite_even_when_model_passes():
    from graph.workflow import route_after_review
    with patch.object(reviewer,'chat',new=AsyncMock(return_value=FakeResp('{"passed":true,"comments":[]}'))):
        out=asyncio.run(reviewer.reviewer_node({'task_id':'t','draft_content':'声明[S999]','evidence':[source()]}))
    assert out['quality_status'] == 'rejected'
    assert not out['forced_output']
    assert route_after_review(out) == 'rewrite'
    assert out['findings']


def test_deleted_source_is_repairable_but_valid_source_is_not_flagged():
    deleted = source()
    deleted['source_deleted'] = True
    body = '旧资料[S1]\n\n未知[S999]\n\n有效[S2]'
    valid = source(source_id='S2', url='https://example.invalid/b')
    findings = reference_findings(body,check_citations('t',body,[deleted,valid]))
    assert [f['location']['paragraph'] for f in findings] == [1,2]
    assert len({f['finding_id'] for f in findings}) == 2
