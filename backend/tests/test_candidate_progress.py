"""R02-02原稿坐标、独立接受和实际进展；不替换被测判断。"""
import asyncio
from unittest.mock import AsyncMock,patch
import pytest
from agents import rewriter
from services.rewrite_patches import apply_partial_patches,paragraph_table
from services.candidate_acceptance import validate_candidate
from services.fact_binding import build_registry
from tests.test_evidence import source
from tests.test_rewriter import FakeResp
import json


def proposals(body, changes):
    rows=paragraph_table(body)
    return dict(mode='patch',patches=[dict(paragraph=n,digest=rows[n-1]['digest'],replacement=text,finding_ids=[f'F{n}']) for n,text in changes],
                results=[dict(finding_id=f'F{n}',status='fixed') for n,_ in changes])


def test_later_patch_uses_original_coordinate_after_deletion():
    body='甲\n\n待删除\n\n待修改\n\n保留'
    findings=[dict(finding_id='F2',location={'paragraph':2}),dict(finding_id='F3',location={'paragraph':3})]
    result=apply_partial_patches(body,proposals(body,[(2,''),(3,'已修改')]),findings)
    assert len(result['accepted'])==2
    assert '待删除' not in result['body'] and '待修改' not in result['body']
    assert result['body'].endswith('已修改\n\n保留')


def test_identical_later_text_cannot_redirect_patch_to_another_target():
    body='标题\n\n删除\n\n相同\n\n相同'
    findings=[dict(finding_id='F2',location={'paragraph':2}),dict(finding_id='F3',location={'paragraph':3})]
    result=apply_partial_patches(body,proposals(body,[(2,''),(3,'第三段修改')]),findings)
    assert result['body'].endswith('第三段修改\n\n相同')


def check(sentence,decision='confirmed'):
    return dict(errors=[],binding_issues=[dict(kind='unbound_number',rule='unbound_number',severity='hard',
        sentence=sentence,numbers=['99亿元'],source_ids=['S1'],origin='program',decision=decision,material=True)])


def test_deterministic_cosmetic_rewrite_is_not_progress():
    with pytest.raises(ValueError,match='进展|目标'):
        validate_candidate('市场规模99亿元。','该市场规模99亿元。',check('市场规模99亿元。'),
                           check('该市场规模99亿元。'),pipeline_version=2,require_progress=True)


def test_rule_downgrade_does_not_count_as_target_resolved():
    with pytest.raises(ValueError,match='进展|目标'):
        validate_candidate('市场规模99亿元。','该市场规模99亿元。',check('市场规模99亿元。'),
                           check('该市场规模99亿元。','suspected'),pipeline_version=2,require_progress=True)


def test_new_semantic_suspicion_is_not_new_confirmed_error():
    before=dict(errors=[],binding_issues=[])
    after=dict(errors=[],binding_issues=[dict(kind='unverified_comparison',severity='hard',decision='suspected',material=True,
                                           sentence='某品牌领先。',source_ids=['S1'])])
    validate_candidate('原稿','改稿',before,after,pipeline_version=2)


def test_missing_reference_identity_does_not_depend_on_error_excerpt():
    before=dict(errors=['含数字段落缺少引用（段落2：市场规模99亿元）'],issues=[dict(paragraph=2,excerpt='市场规模99亿元。',tokens=['99亿元'])],binding_issues=[])
    after=dict(errors=['含数字段落缺少引用（段落9：该市场规模99亿元）'],issues=[dict(paragraph=9,excerpt='该市场规模99亿元。',tokens=['99亿元'])],binding_issues=[])
    validate_candidate('市场规模99亿元。','该市场规模99亿元。',before,after,pipeline_version=2)


def test_mixed_v2_binding_failure_keeps_other_valid_patch_without_full_upgrade():
    evidence=[source(excerpt='市场规模10亿元。市场增长20%。')]
    registry=build_registry('t',evidence)
    first=next(key for key,fact in registry.items() if '10亿元' in fact['quote'])
    body='# 报告\n\n市场规模99亿元[S1]。\n\n市场增长98%[S1]。'
    task=dict(task_id='t',topic='市场',report_pipeline_version=2,draft_content=body,evidence=evidence,
        findings=[dict(finding_id=f'F{n}',rule='unbound_number',severity='hard',location={'paragraph':n,'sentence':1},numbers=[value])
                  for n,value in [(2,'99亿元'),(3,'98%')]])
    payload=proposals(body,[(2,f'市场规模10亿元[{first}]。'),(3,f'市场增长999%[{first}]。')])
    model=AsyncMock(return_value=FakeResp(json.dumps(payload,ensure_ascii=False)))
    with patch.object(rewriter,'chat',new=model):
        result=asyncio.run(rewriter.rewriter_node(task))
    assert '10亿元[S1]' in result['draft_content'] and '99亿元' not in result['draft_content']
    assert '98%' in result['draft_content'] and '999%' not in result['draft_content']
    assert model.await_count==1
    assert len(result['review_history'][-1]['rewrite']['attempts'][0]['accepted'])==1


def test_explicit_patch_mapping_keeps_rewritten_target_after_prior_deletion():
    from services.issue_identity import index_document,index_candidate
    body='甲\n\n删除\n\n修改前'
    old=index_document('t',body)
    payload=proposals(body,[(2,''),(3,'修改后')])
    findings=[dict(finding_id=f'F{n}',location={'paragraph':n}) for n in (2,3)]
    partial=apply_partial_patches(body,payload,findings)
    current=index_candidate('t',body,partial['body'],old,patch=True,edits=partial['accepted'])
    assert current['blocks'][1]['target_id']==old['blocks'][2]['target_id']
    assert current['blocks'][1]['text']=='修改后'


def test_failed_full_dependency_group_does_not_apply_valid_half():
    from tests.test_rewriter_patch_flow import state
    task=state()
    task['full_rewrite_required']=True
    body=task['draft_content']
    full=dict(mode='full',reason='关联修改',content=body.replace('99亿元','10亿元')+'\n\n新增999亿元[S1]。',results=[])
    model=AsyncMock(return_value=FakeResp(json.dumps(full,ensure_ascii=False)))
    with patch.object(rewriter,'chat',new=model):
        result=asyncio.run(rewriter.rewriter_node(task))
    assert result['draft_content']==body
    assert not result['review_history'][-1]['rewrite']['changed']


def test_explicit_patch_ownership_wins_over_matching_old_text():
    from services.issue_identity import index_document, index_candidate
    body = '甲\n\n乙'
    old = index_document('t', body)
    edits = [dict(paragraph=1, replacement='乙'), dict(paragraph=2, replacement='甲')]
    current = index_candidate('t', body, '乙\n\n甲', old, patch=True, edits=edits)
    assert [b['target_id'] for b in current['blocks']] == [b['target_id'] for b in old['blocks']]


@pytest.mark.parametrize(('after', 'edits', 'message'), [
    ('别的正文', [dict(paragraph=1, replacement='改稿')], '实际候选'),
    ('甲\n\n乙\n\n丙', [dict(paragraph=1, replacement='甲\n\n乙')], '无法唯一确定'),
])
def test_patch_mapping_rejects_inconsistent_or_split_actions(after, edits, message):
    from services.issue_identity import index_document, index_candidate
    body = '甲\n\n丙'
    with pytest.raises(ValueError, match=message):
        index_candidate('t', body, after, index_document('t', body), patch=True, edits=edits)


@pytest.mark.parametrize(('rule', 'decision'), [
    ('unsupported_wording', 'confirmed'), ('unverified_comparison', 'confirmed'),
    ('unbound_number', 'suspected'), ('unbound_number', 'unresolved'),
])
def test_semantic_or_pending_receipt_requires_review(rule, decision):
    from services.issue_identity import index_document
    from services.rewrite_verification import verify_findings
    body = '这是分析意见。'
    index = index_document('t', body)
    finding = dict(finding_id='F1', rule=rule, decision=decision,
                   target_id=index['blocks'][0]['target_id'], location={'paragraph':1,'sentence':1})
    rows, corrections = verify_findings(body, [], [finding], [{'finding_id':'F1','status':'fixed'}],
                                        task_id='t', content_index=index, strict_identity=True)
    assert rows[0]['status'] == 'unverified'
    assert corrections == []
