"""有效补丁不因其他补丁或回执问题被整批丢弃。"""
import asyncio
import json
from unittest.mock import AsyncMock, patch
from agents import rewriter
from services.rewrite_patches import apply_partial_patches, paragraph_table
from tests.test_rewriter_patch_flow import state, response
from tests.test_rewriter import FakeResp

def test_partial_keeps_valid_patch_with_missing_receipts():
    body = '第一段\n\n第二段'
    good = dict({k:v for k,v in paragraph_table(body)[0].items() if k != 'text'}, replacement='修改一', finding_ids=['F1'])
    bad = dict({k:v for k,v in paragraph_table(body)[1].items() if k != 'text'}, digest='wrong', replacement='修改二', finding_ids=['F2'])
    findings = [{'finding_id':'F1','location':{'paragraph':1}}, {'finding_id':'F2','location':{'paragraph':2}}]
    out = apply_partial_patches(body, {'mode':'patch','patches':[good,bad]}, findings)
    assert out['body'] == '修改一\n\n第二段'
    assert len(out['rejected']) == 1
    assert out['receipt_errors']
    assert [x['status'] for x in out['results']] == ['fixed','cannot_fix']

def test_missing_receipt_does_not_discard_valid_model_edit():
    s = state()
    payload = json.loads(response(s, '市场规模10亿元。[S1]').choices[0].message.content)
    payload['results'] = []
    with patch.object(rewriter,'chat',new=AsyncMock(return_value=FakeResp(json.dumps(payload)))) as chat:
        out = asyncio.run(rewriter.rewriter_node(s))
    assert chat.await_count == 1
    assert '10亿元' in out['draft_content']
    assert out['forced_output'] is False
    assert out['quality_status'] == 'rejected'
    assert out['review_history'][-1]['rewrite']['receipt_errors']

def test_candidate_with_remaining_old_issue_reaches_review():
    s = state()
    full = {'mode':'full','reason':'补丁失败','content':'# 修改标题\n\n市场规模99亿元。[S1]', 'results':[]}
    with patch.object(rewriter,'chat',new=AsyncMock(side_effect=[FakeResp('{}'),FakeResp('{}'),FakeResp(json.dumps(full))])):
        out = asyncio.run(rewriter.rewriter_node(s))
    assert out['draft_content'] == s['draft_content']
    assert out['review_history'][-1]['rewrite']['attempts'][-1]['message'] == '全文候选未解决目标硬问题'
    assert out['forced_output'] is True
    assert out['quality_status'] == 'rejected'


def test_matching_duplicate_finding_keeps_deterministic_identity():
    from services.findings import merge_findings
    from tests.test_evidence import source
    base = {'finding_id':'BIND-1','location':{'paragraph':1,'sentence':1},
            'expected':'改引S1','problem':'引用错误','severity':'hard',
            'evidence':{'source_id':'S1','quote':'市场规模10亿元。'}}
    duplicate = dict(base, finding_id='BIND-1-FIX-FAIL')
    merged, rejected = merge_findings([base], [duplicate], [source(excerpt='市场规模10亿元。')])
    assert not rejected
    assert [f['finding_id'] for f in merged] == ['BIND-1']


def test_saved_real_responses_produce_candidate_without_online_calls():
    from pathlib import Path
    saved = json.loads((Path(__file__).parent / 'fixtures/reference_audit/rewrite_failed.json').read_text(encoding='utf-8'))
    saved['review_comments'] = []
    with patch.object(rewriter, 'chat', new=AsyncMock(side_effect=[FakeResp(x) for x in saved['responses']])):
        out = asyncio.run(rewriter._rewrite_patch(saved, saved['draft_content'], saved['findings'],
                                                [{'role':'system','content':'离线回放'}], [], []))
    assert out['draft_content'] != saved['draft_content']
    assert not out['forced_output']
    assert out['quality_status'] == 'rejected'
    assert out['review_history'][-1]['rewrite']['applied'] is True


def test_changed_candidate_is_reviewed_in_real_graph(monkeypatch):
    from graph import workflow
    async def planner(s):
        return {}
    async def scheduler(s):
        return {'draft_content':'# 报告\n\n原稿','plan':[], 'current_step_index':0}
    reviews = []
    async def reviewer_node(s):
        reviews.append(s['draft_content'])
        return {'quality_status':'rejected' if len(reviews) == 1 else 'passed', 'passed':len(reviews) > 1}
    async def rewrite_node(s):
        return {'draft_content':'# 报告\n\n候选稿', 'forced_output':False, 'quality_status':'rejected'}
    monkeypatch.setattr(workflow,'planner_node',planner)
    monkeypatch.setattr(workflow,'scheduler_step_node',scheduler)
    monkeypatch.setattr(workflow,'route_after_step',lambda s:'reviewer')
    monkeypatch.setattr(workflow,'reviewer_node',reviewer_node)
    monkeypatch.setattr(workflow,'rewriter_node',rewrite_node)
    out = asyncio.run(workflow.build_graph().ainvoke(workflow._initial_state('t','主题')))
    assert reviews == ['# 报告\n\n原稿','# 报告\n\n候选稿']
    assert out['quality_status'] == 'passed'


def test_all_invalid_responses_stop_without_changing_draft():
    s = state()
    with patch.object(rewriter, 'chat', new=AsyncMock(return_value=FakeResp('{}'))) as chat:
        out = asyncio.run(rewriter.rewriter_node(s))
    assert chat.await_count == 3
    assert out['draft_content'] == s['draft_content']
    assert out['forced_output'] is True
    assert out['review_history'][-1]['stop_reason'] == 'REWRITE_FAILED'
    assert out['quality_status'] == 'rejected'
