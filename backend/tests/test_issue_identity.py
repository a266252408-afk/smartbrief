"""R02-01稳定目标与问题身份，不从措辞或坐标推导修复成功。"""
import pytest
from services.candidate_acceptance import hard_problem_counts
from services.review_progress import review_progress


def issue(text='市场规模约99亿元。', **extra):
    return dict(kind='unbound_number', rule='unbound_number', severity='hard',
                sentence=text, numbers=['99亿元'], source_ids=['S1'], **extra)


def test_d03_problem_counts_ignore_deictic_rewording():
    assert hard_problem_counts({'binding_issues':[issue()]}) == hard_problem_counts({'binding_issues':[issue('该市场规模约99亿元。')]})


def test_same_value_for_different_subjects_is_not_one_problem():
    assert hard_problem_counts({'binding_issues':[issue('苹果市场规模99亿元。')]}) != hard_problem_counts({'binding_issues':[issue('华为市场规模99亿元。')]})


def test_problem_description_change_does_not_fake_progress():
    old = issue(problem='数字未被支持')
    new = issue(problem='该数字缺少来源支持')
    result = review_progress({'hard_finding_set':[old], 'review_complete':True, 'rewrite':{'changed':True}},[new],True)
    assert result['persisting'] and not result['introduced'] and not result['resolved']
    assert result['stopped']


def test_occurrences_counted_individually():
    item = issue()
    result = review_progress({'hard_finding_set':[item,item], 'review_complete':True,'rewrite':{'changed':True}},[item],True)
    assert result['previous_problem_count'] == 2 and result['current_problem_count'] == 1
    assert result['resolved'] and not result['stopped']


def test_changed_period_is_introduced_even_with_same_value():
    a, b = issue('2024年市场规模99亿元。'), issue('2025年市场规模99亿元。')
    result = review_progress({'hard_finding_set':[a]},[b],True)
    assert result['resolved'] and result['introduced']


def test_server_targets_survive_moves_and_explicit_rewrite():
    from services.issue_identity import index_document
    original = index_document('t','甲段\n\n乙段')
    moved = index_document('t','乙段\n\n甲段',original)
    assert moved['blocks'][0]['target_id'] == original['blocks'][1]['target_id']
    replaced = index_document('t','改写乙段\n\n甲段',moved,
        target_mapping={moved['blocks'][0]['target_id']:1,moved['blocks'][1]['target_id']:2},base_revision=moved['revision'])
    assert replaced['blocks'][0]['target_id'] == moved['blocks'][0]['target_id']
    assert replaced['blocks'][0]['certainty'] == 'mapped'
    assert index_document('t','改写乙段\n\n甲段',replaced)['revision'] == replaced['revision']


def test_unknown_rewrite_and_ambiguous_duplicates_are_unverified():
    from services.issue_identity import index_document
    old = index_document('t','相同段\n\n相同段')
    new = index_document('t','相同段',old)
    assert new['blocks'][0]['certainty'] == 'unverified'
    rewritten = index_document('t','全部改写',old)
    assert rewritten['blocks'][0]['certainty'] == 'unverified'


def test_content_index_rejects_cross_task_stale_and_ambiguous_mapping():
    from services.issue_identity import index_document
    old = index_document('t','甲\n\n乙')
    with pytest.raises(ValueError):
        index_document('other','甲',old)
    with pytest.raises(ValueError):
        index_document('t','变',old,base_revision=0)
    with pytest.raises(ValueError):
        index_document('t','变\n\n乙',old,target_mapping={b['target_id']:1 for b in old['blocks']})


def test_issue_identity_keeps_target_and_occurrence_not_location_or_problem():
    from services.issue_identity import index_document, attach_identities
    body='市场规模99亿元。\n\n市场规模99亿元。'
    index=index_document('t',body)
    findings=[dict(issue(),location={'paragraph':n,'sentence':1}) for n in (1,2)]
    old=attach_identities(body,findings,index)
    assert old[0]['issue_id'] != old[1]['issue_id']
    moved=index_document('t',body,index,target_mapping={})
    current=attach_identities(body,[dict(f,problem='新措辞') for f in findings],moved)
    assert [f['issue_id'] for f in old] == [f['issue_id'] for f in current]


def test_equivalent_numeric_units_keep_identity_without_rounding_away_changes():
    a=issue()
    b=issue('市场规模约990000万元。')
    b['numbers']=['990000万元']
    assert hard_problem_counts({'binding_issues':[a]}) == hard_problem_counts({'binding_issues':[b]})
    wrong=issue('市场规模约9900万元。')
    wrong['numbers']=['9900万元']
    assert hard_problem_counts({'binding_issues':[a]}) != hard_problem_counts({'binding_issues':[wrong]})
    c=issue('市场规模约99.1亿元。')
    c['numbers']=['99.1亿元']
    assert hard_problem_counts({'binding_issues':[a]}) != hard_problem_counts({'binding_issues':[c]})


def test_unknown_target_mapping_cannot_claim_resolved_or_introduced():
    from services.issue_identity import index_document,attach_identities
    before='市场规模约99亿元。'
    after='该市场规模约99亿元。'
    old_index=index_document('t',before)
    old=attach_identities(before,[dict(issue(),location={'paragraph':1,'sentence':1})],old_index)
    new=attach_identities(after,[dict(issue(after),location={'paragraph':1,'sentence':1})],index_document('t',after,old_index))
    result=review_progress({'issue_set':old,'review_complete':True,'rewrite':{'changed':True}},new,True)
    assert result['unverified'] and not result['resolved'] and not result['introduced']
    assert not result['stopped']


def test_unknown_rewrite_with_changed_surface_fields_cannot_claim_old_resolved():
    from services.issue_identity import index_document,attach_identities
    before='市场规模约99亿元。'
    after='资料显示，该市场规模约99亿元。'
    index=index_document('t',before)
    old=attach_identities(before,[dict(issue(),location={'paragraph':1,'sentence':1})],index)
    new=attach_identities(after,[dict(issue(after),location={'paragraph':1,'sentence':1})],index_document('t',after,index))
    result=review_progress({'issue_set':old,'review_complete':True,'rewrite':{'changed':True}},new,True)
    assert result['unverified'] and not result['resolved'] and not result['introduced']


def test_explicit_mapping_reserves_ids_before_exact_text_matching():
    from services.issue_identity import index_document
    old=index_document('t','甲\n\n乙')
    new=index_document('t','甲\n\n改乙',old,target_mapping={old['blocks'][0]['target_id']:2},base_revision=old['revision'])
    assert len({b['target_id'] for b in new['blocks']}) == 2
    assert new['blocks'][1]['target_id'] == old['blocks'][0]['target_id']


def test_bound_deletion_keeps_candidate_when_blank_paragraphs_collapse():
    import asyncio
    from unittest.mock import AsyncMock,patch
    from agents import rewriter
    from services.issue_identity import index_document,attach_identities
    from tests.test_rewriter_patch_flow import state,response
    task=state()
    task['draft_content']='# 标题\n\n市场规模99亿元[S1]。\n\n市场规模10亿元[S1]。'
    task['report_pipeline_version']=2
    task['content_index']=index_document('t',task['draft_content'])
    task['findings']=attach_identities(task['draft_content'],task['findings'],task['content_index'],task['evidence'])
    model=AsyncMock(return_value=response(task,''))
    with patch.object(rewriter,'chat',new=model):
        result=asyncio.run(rewriter.rewriter_node(task))
    assert '99亿元' not in result['draft_content']
    assert '10亿元' in result['draft_content']
    assert model.await_count == 1


def test_patch_exact_moves_keep_source_identity():
    from services.issue_identity import index_document,index_candidate
    old=index_document('t','甲\n\n乙')
    new=index_candidate('t','甲\n\n乙','乙\n\n甲',old,patch=True)
    assert new['blocks'][0]['target_id']==old['blocks'][1]['target_id']
    assert new['blocks'][1]['target_id']==old['blocks'][0]['target_id']


def test_global_integrity_identity_uses_error_category_and_source():
    from services.issue_identity import citation_issue,identity_key
    a=citation_issue('[S9]来源不存在或不属于当前任务','t')
    b=citation_issue('[S9]来源不存在','t')
    assert identity_key(a)==identity_key(b)
    assert identity_key(a)!=identity_key(citation_issue('[S10]来源不存在','t'))
    assert citation_issue('无法识别的完整性故障','t')['identity_certainty']=='unverified'


def test_exact_number_identity_does_not_round_long_values_together():
    a=issue('市场规模12345678901234567890123456781亿元。')
    a['numbers']=['12345678901234567890123456781亿元']
    b=issue('市场规模12345678901234567890123456782亿元。')
    b['numbers']=['12345678901234567890123456782亿元']
    assert hard_problem_counts({'binding_issues':[a]}) != hard_problem_counts({'binding_issues':[b]})


@pytest.mark.parametrize('before,after', [('## 甲章节\n\n甲内容\n\n## 乙章节\n\n乙内容','## 乙章节\n\n乙内容\n\n## 甲章节\n\n甲内容'),
    ('甲市场规模99亿元[S1]。\n\n乙市场规模98亿元[S1]。','乙市场规模98亿元[S1]。\n\n甲市场规模99亿元[S1]。')])
def test_global_structure_problem_is_not_tied_to_first_paragraph(before,after):
    from services.issue_identity import index_document,attach_identities
    old_index=index_document('t',before)
    finding=dict(rule='required_structure',kind='required_structure',structure_kind='missing_title',
                 severity='hard',location={'paragraph':1,'sentence':1})
    old=attach_identities(before,[finding],old_index)
    current=attach_identities(after,[finding],index_document('t',after,old_index))
    assert old[0]['issue_id'] == current[0]['issue_id']


def test_missing_source_cannot_be_reported_as_resolved_binding():
    from services.issue_identity import index_document,attach_identities
    from services.rewrite_verification import verify_findings
    from tests.test_evidence import source
    body='市场规模99亿元[S1]。'
    index=index_document('t',body)
    finding=attach_identities(body,[dict(issue(),finding_id='BIND-1',location={'paragraph':1,'sentence':1})],index,[source(excerpt='市场规模10亿元。')])[0]
    rows,_=verify_findings(body,[],[finding],[{'finding_id':'BIND-1','status':'fixed'}],task_id='t',content_index=index,strict_identity=True)
    assert rows[0]['status']=='unverified'


def test_source_integrity_failure_does_not_fake_old_problem_resolution():
    from services.issue_identity import index_document,attach_identities
    body='市场规模99亿元[S1]。'
    index=index_document('t',body)
    old=attach_identities(body,[dict(issue(),location={'paragraph':1,'sentence':1})],index)
    new=attach_identities(body,[dict(kind='invalid_reference',rule='invalid_reference',severity='hard',
        location={'paragraph':1,'sentence':1})],index)
    result=review_progress({'issue_set':old,'review_complete':True,'rewrite':{'changed':True}},new,True)
    assert result['unverified'] and not result['resolved']
