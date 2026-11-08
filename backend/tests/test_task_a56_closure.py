"""真实task_a56边界回归；模型仅在外部调用边界使用离线替身。"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agents import reviewer, rewriter
from services.citations import check_citations, citation_lifecycle
from services.findings import merge_findings
from tests.test_evidence import source
from tests.test_rewriter import FakeResp

SAVED = json.loads((Path(__file__).parent / 'fixtures/reference_audit/task_a56.json').read_text(encoding='utf-8'))


def test_actual_review_ellipsis_keeps_precise_advice_but_rejects_invented_middle():
    item = SAVED['reviews'][1]['rejected_findings'][0]['finding']
    merged, rejected = merge_findings([], [item], SAVED['evidence'], SAVED['drafts'][1])
    assert not rejected and '60.23%' in merged[0]['expected']
    bad = dict(item, evidence=dict(item['evidence'], quote=item['evidence']['quote'].replace('73.10%', '99.99%')))
    assert merge_findings([], [bad], SAVED['evidence'])[1]


@pytest.mark.parametrize(('claim', 'bad'), [
    ('2024年中国消费者使用智能可穿戴设备的运动监测比例超过60%[S1]。', False),
    ('2024年中国消费者使用智能可穿戴设备的运动监测比例60%以上[S1]。', False),
    ('2024年中国消费者使用智能可穿戴设备的运动监测比例61%以上[S1]。', True),
    ('2024年中国消费者使用智能可穿戴设备的运动监测比例60%以下[S1]。', True),
    ('2024年中国消费者使用智能可穿戴设备的运动监测比例60%[S1]。', True),
    ('2024年中国消费者使用智能可穿戴设备的运动监测比例70%以上[S1]。', True),
    ('2026年中国消费者使用智能可穿戴设备的运动监测比例60%以上[S1]。', True),
    ('2024年中国消费者使用智能手表的运动监测比例60%以上[S1]。', True),
])
def test_percentage_bounds_keep_metric_population_and_period(claim, bad):
    e = source(excerpt='2024年中国消费者使用智能可穿戴设备场景认知中，73.10%选择健康检测，60.23%选择运动检测。')
    issues = check_citations('t', claim, [e])['binding_issues']
    assert bool([i for i in issues if i.get('severity') == 'hard']) is bad


def test_real_length_and_extra_section_demands_trigger_review_correction():
    invalid = {'passed': False, 'comments': SAVED['reviews'][0]['comments'],
               'findings': [SAVED['reviews'][0]['hard_finding_set'][0]], 'full_rewrite_required': True}
    invalid['findings'][0] = {k: v for k, v in invalid['findings'][0].items() if k not in ['rule', 'kind']}
    with patch.object(reviewer, 'chat', new=AsyncMock(side_effect=[FakeResp(json.dumps(invalid)),
            FakeResp('{"passed":true,"comments":[],"findings":[]}')])) as chat:
        out = asyncio.run(reviewer.reviewer_node(dict(task_id=SAVED['provenance']['task_id'], draft_content=SAVED['drafts'][0],
            evidence=SAVED['evidence'], max_iterations=3, research_revision_count=1)))
    assert chat.await_count == 2
    assert '4839' in chat.call_args_list[0].args[0][1]['content']
    assert out['quality_status'] == 'passed'
    assert out['review_history'][-1]['policy_corrections']


def test_repeated_invalid_review_policy_is_unavailable_not_a_rewrite_demand():
    draft = '# 报告\n\n## 执行摘要\n' + '正文内容' * 300 + '\n\n## 结论与建议\n结论'
    bad = FakeResp(json.dumps({'passed':False, 'comments':['必须添加独立标题页'], 'findings':[]}))
    with patch.object(reviewer, 'chat', new=AsyncMock(return_value=bad)) as chat:
        out = asyncio.run(reviewer.reviewer_node(dict(task_id='t', draft_content=draft, evidence=[])))
    assert chat.await_count == 2
    assert out['quality_status'] == 'unavailable' and not out['full_rewrite_required']


def test_rejected_quote_does_not_force_full_and_comments_are_not_cut_to_five():
    body = '# 报告\n\n## 执行摘要\n摘要\n\n## 正文\n内容\n\n## 结论与建议\n结论'
    finding = dict(finding_id='MODEL-1', problem='待核对', expected='核对', severity='hard',
        location={'paragraph':3,'sentence':1}, evidence={'source_id':'S1','quote':'不存在的文本'})
    payload = {'passed':False,'comments':[f'意见{i}' for i in range(8)],'findings':[finding]}
    with patch.object(reviewer, 'chat', new=AsyncMock(return_value=FakeResp(json.dumps(payload)))):
        out = asyncio.run(reviewer.reviewer_node(dict(task_id='t', draft_content=body, evidence=[source()], research_revision_count=1)))
    assert not out['full_rewrite_required']
    assert '意见7' in out['review_comments']


def test_explicit_qualitative_deletion_receipt_is_preserved_for_review():
    before = '健康意识增强使续航要求提升，这会带动换代需求[S27]。\n\n其他研究结论。'
    after = '其他研究结论。'
    changes = [{'source_id':'S27','original_claim':before.split('\n')[0],'action':'deleted'}]
    life = citation_lifecycle(before, after, ['S27'], changes=changes)
    assert life['retired_ids'] == ['S27']
    assert life['retired_claims'][0]['review_required'] is True
    # 保留原声明但删引用、伪造原句、无回执均不能退休。
    assert not citation_lifecycle(before, before.replace('[S27]', ''), ['S27'], changes=changes)['retired_ids']
    assert not citation_lifecycle(before, after, ['S27'], changes=[dict(changes[0],original_claim='伪造')])['retired_ids']
    assert not citation_lifecycle(before, after, ['S27'])['retired_ids']


def test_fixed_receipt_with_persisting_numeric_issue_gets_one_targeted_correction():
    draft = '# 报告\n\n市场规模99亿元[S1]。\n\n## 结论与建议\n结论\n\n市场规模99亿元[S1]。'
    finding = dict(finding_id='BIND-1',rule='unbound_number',kind='unbound_number',severity='hard',
        problem='错误数字',expected='核对数字',numbers=['99亿元'],cited=['S1'],
        location={'paragraph':2,'sentence':1},evidence={'source_id':'S1','quote':'市场规模10亿元。'})
    payload = {'mode':'full','content':draft.rsplit('\n\n', 1)[0], 'reason':'已删除一处重复错误，待修剩余位置',
               'results':[{'finding_id':'BIND-1','status':'fixed','reason':'删措辞'}]}
    async def respond(messages, **kwargs):
        instruction = json.loads(messages[-1]['content'])
        if instruction.get('verification_feedback'):
            assert instruction['mode'] == 'patch'
            assert instruction['verification_feedback'][0]['finding_id'] == 'BIND-1'
            p = next(p for p in instruction['paragraphs'] if '99亿元' in p['text'])
            return FakeResp(json.dumps({'mode':'patch','patches':[dict(paragraph=p['paragraph'],digest=p['digest'],
                replacement='市场规模10亿元[S1]。',finding_ids=['BIND-1'])],
                'results':[{'finding_id':'BIND-1','status':'fixed','reason':'修正数值'}]}))
        return FakeResp(json.dumps(payload))
    state = dict(task_id='t', draft_content=draft, findings=[finding], evidence=[source(excerpt='市场规模10亿元。')],
                 full_rewrite_required=True, review_history=[{'round':1,'findings':[finding]}])
    with patch.object(rewriter,'chat',new=AsyncMock(side_effect=respond)) as chat:
        out = asyncio.run(rewriter.rewriter_node(state))
    assert chat.await_count == 2 and '99亿元' not in out['draft_content']
    audit = out['review_history'][-1]['rewrite']
    assert audit['attempts'][0]['verification'][0]['status'] == 'unresolved'
    assert audit['verification'][0]['status'] == 'resolved'
    assert out['quality_status'] == 'rejected'  # 候选修复不能替代独立复审。


def test_failed_correction_preserves_applied_candidate_and_is_bounded():
    from tests.test_rewriter_patch_flow import state
    s = state()
    s['full_rewrite_required'] = True
    candidate = s['draft_content'].replace('标题', '研究标题')
    response = FakeResp(json.dumps({'mode':'full','content':candidate,'reason':'改标题',
        'results':[{'finding_id':f['finding_id'],'status':'fixed','reason':'改标题'} for f in s['findings']]}))
    with patch.object(rewriter,'chat',new=AsyncMock(side_effect=[response,RuntimeError('离线故障')])) as chat:
        out = asyncio.run(rewriter.rewriter_node(s))
    assert chat.await_count == 1 and out['draft_content'] == s['draft_content']
    assert out['review_history'][-1]['rewrite']['attempts'][0]['message'] == '全文候选未解决目标硬问题'


def test_qualitative_obligation_survives_rounds_and_must_be_reviewed():
    original = '健康意识增强使续航要求提升，这会带动换代需求[S27]。'
    first = citation_lifecycle(original, '其他结论。', ['S27'])
    changes = [{'source_id':'S27','original_claim':original,'action':'deleted'}]
    second = citation_lifecycle('其他结论。', '其他结论。', ['S27'], changes=changes,
                                original_claims=first['retained_claims'])
    assert second['retired_ids'] == ['S27']
    state = dict(task_id='t', draft_content='健康检测比例73.10%[S1]。', evidence=[source(excerpt='健康检测比例73.10%。')],
        review_history=[{'round':1,'rewrite':{'citation_lifecycle':second}}])
    with patch.object(reviewer,'chat',new=AsyncMock(return_value=FakeResp('{"passed":true,"comments":[]}'))) as chat:
        out = asyncio.run(reviewer.reviewer_node(state))
    assert original in chat.call_args.args[0][1]['content']
    assert out['review_history'][-1]['citation_retirements'][0]['review_required']


def test_real_full_rewrite_and_retired_claims_survive_graph_and_repository(monkeypatch):
    from graph import workflow
    from db import task_repository as repo
    from db.database import init_db
    init_db()
    tid = SAVED['provenance']['task_id']
    repo.create_task(tid, '智能手表离线回归')
    async def planner(s):
        return {'plan':[]}
    async def writer(s):
        return {'draft_content':SAVED['drafts'][0], 'evidence':SAVED['evidence']}
    monkeypatch.setattr(workflow, 'planner_node', planner)
    monkeypatch.setattr(workflow, 'scheduler_step_node', writer)
    monkeypatch.setattr(workflow, 'route_after_step', lambda s:'reviewer')
    monkeypatch.setattr(workflow, '_remember_task', AsyncMock())
    first = FakeResp(json.dumps({'passed':False,'comments':['统一标题层级'], 'full_rewrite_required':True}))
    monkeypatch.setattr(reviewer,'chat',AsyncMock(side_effect=[first,FakeResp('{"passed":true,"comments":[]}')]))
    # 正文取真实末稿，仅按S1把“超过80%”改为“近80%”；审核通过是离线替身结果。
    claims = citation_lifecycle(SAVED['drafts'][0], SAVED['drafts'][0], ['S27'])['retained_claims']['S27']
    payload = {'mode':'full','content':SAVED['drafts'][2].replace('超过80%', '近80%'),'reason':'修复层级并删除原定性声明', 'results':[],
               'citation_changes':[{'source_id':'S27','original_claim':c,'action':'deleted'} for c in claims]}
    monkeypatch.setattr(rewriter,'chat',AsyncMock(return_value=FakeResp(json.dumps(payload))))
    out = asyncio.run(workflow.run_task(tid, '智能手表离线回归'))
    assert len(out['review_history']) == 2 and out['quality_status'] == 'passed'
    saved = repo.get_task(tid)
    assert saved['review_history'][0]['rewrite']['citation_lifecycle']['retired_ids'] == ['S27']
    assert saved['review_history'][1]['citation_retirements'][0]['claims'] == claims


def test_changed_wrong_number_cannot_be_called_resolved():
    from services.rewrite_verification import verify_findings
    finding = {'finding_id':'BIND-1','rule':'unbound_number','numbers':['99亿元'],'location':{'paragraph':1}}
    rows, targets = verify_findings('市场规模98亿元[S1]。', [source(excerpt='市场规模10亿元。')], [finding],
        [{'finding_id':'BIND-1','status':'fixed'}],task_id='t')
    assert rows[0]['status'] == 'unresolved' and targets


def test_citation_only_receipt_can_continue_to_independent_review():
    claim = '健康意识增强使续航要求提升，这会带动换代需求[S27]。'
    draft = '# 报告\n\n市场规模10亿元[S1]。\n\n## 结论与建议\n结论'
    state = dict(task_id='t',draft_content=draft,evidence=[source(excerpt='市场规模10亿元。'),
        source(source_id='S27',url='https://example.invalid/27',excerpt=claim)],required_citations=['S1','S27'],
        full_rewrite_required=True, review_history=[{'round':1,'rewrite':{'citation_lifecycle':{'retained_claims':{'S27':[claim]}}}}])
    response = FakeResp(json.dumps({'mode':'full','content':draft,'reason':'原声明已删除，补充删除回执','results':[],
        'citation_changes':[{'source_id':'S27','original_claim':claim,'action':'deleted'}]}))
    with patch.object(rewriter,'chat',new=AsyncMock(return_value=response)):
        out = asyncio.run(rewriter.rewriter_node(state))
    assert out['forced_output'] is False and out['draft_content'] == draft
    assert out['review_history'][-1]['rewrite']['citation_changes_applied'] is True


def test_broad_or_approximate_source_cannot_prove_a_precise_bound():
    from services.numeric_claims import claim_token_matches
    assert not claim_token_matches('60%', '运动比例60%以上', '运动比例约60.23%')
    assert not claim_token_matches('60%', '比例60%以上', '健康比例73.10%')
    assert not claim_token_matches('60%', '中国运动比例60%以上', '欧洲运动比例60.23%')
    assert not claim_token_matches('60%', 'Q2运动比例60%以上', 'Q1运动比例60.23%')
    assert claim_token_matches('61%', '运动比例61%以下', '运动比例60.23%')
    assert claim_token_matches('60%', '运动比例超过60%', '运动比例超过60%')
    assert not claim_token_matches('60%', '运动比例超过60%', '运动比例60%以上')
    assert not claim_token_matches('60%', '运动比例60%以上', '运动比例60%以下')
    assert not claim_token_matches('60%', '2026年运动比例60%以上', '2026年市场展望。2024年运动比例60.23%')
    assert not claim_token_matches('60%', '中国运动比例60%以上', '中国市场展望。欧洲运动比例60.23%')
    assert not claim_token_matches('60%', '智能手表运动比例60%以上', '智能手表展望。可穿戴设备运动比例60.23%')


def test_qualitative_paraphrase_and_partial_receipts_cannot_retire_a_source():
    before = '健康意识增强使续航要求提升，这会带动换代需求[S27]。\n\n消费者愿意支付更高价格[S27]。'
    claims = citation_lifecycle(before,before,['S27'])['retained_claims']['S27']
    changes = [{'source_id':'S27','original_claim':c,'action':'deleted'} for c in claims]
    assert not citation_lifecycle(before,'健康意识增强使续航要求提高，这会拉动换代需求。',['S27'],changes=changes)['retired_ids']
    assert not citation_lifecycle(before,'其他结论。',['S27'],changes=changes[:1])['retired_ids']
