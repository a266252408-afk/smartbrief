"""返工范围升级与程序拦截的可追溯行为。"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

from agents import rewriter
from services.rewrite_patches import paragraph_table
from tests.test_rewriter_patch_flow import state, response
from tests.test_rewriter import FakeResp


def full(content):
    return FakeResp(json.dumps({'mode': 'full', 'content': content,
                               'reason': '整体重组', 'results': []}))


def test_requested_full_is_not_vetoed_by_complete_skeleton():
    s = state()
    s.update(full_rewrite_required=True, draft_content=(
        '# 报告\n\n## 执行摘要\n摘要\n\n## 正文\n重复\n\n## 结论与建议\n结论'))
    with patch.object(rewriter, 'chat', new=AsyncMock(return_value=full('# 报告\n\n已重组正文'))) as chat:
        out = asyncio.run(rewriter.rewriter_node(s))
    request = json.loads(chat.call_args.args[0][-1]['content'])
    assert request['mode'] == 'full'
    assert '只修改点名段落' not in request['contract']
    assert chat.await_count == 1
    assert out['draft_content'] == '# 报告\n\n已重组正文'
    assert out['quality_status'] == 'rejected'


def test_partial_scope_rejection_escalates_and_retains_proposal_and_decision():
    s = state()
    payload = json.loads(response(s, '市场规模10亿元。[S1]').choices[0].message.content)
    extra = paragraph_table(s['draft_content'])[2]
    payload['patches'].append({k: v for k, v in dict(extra, replacement='重组后的结论',
                                                  finding_ids=['BIND-0001']).items() if k != 'text'})
    with patch.object(rewriter, 'chat', new=AsyncMock(side_effect=[
        FakeResp(json.dumps(payload)), full('# 报告\n\n市场规模10亿元。[S1]\n\n重组后的结论')])) as chat:
        out = asyncio.run(rewriter.rewriter_node(s))
    receipt = out['review_history'][-1]['rewrite']
    assert chat.await_count == 2
    assert receipt['mode'] == 'full'
    assert '重组后的结论' in out['draft_content']
    assert receipt['program_restriction']['observed'] is True
    assert receipt['program_restriction']['outcome'] == 'candidate_applied_after_restriction'
    attempt = receipt['attempts'][0]
    assert attempt['rejected'][0]['code'] == 'SCOPE_RESTRICTED'
    assert attempt['rejected'][0]['proposal']['replacement'] == '重组后的结论'
    assert attempt['accepted'][0]['paragraph'] == 2
    assert attempt['raw_sha256'] and attempt['request']['allowed_paragraphs'] == [2]
    assert attempt['request']['evidence_ids'] == ['S1']


def test_failed_full_keeps_partial_and_explains_unapplied_changes():
    s = state()
    payload = json.loads(response(s, '市场规模10亿元。[S1]').choices[0].message.content)
    payload['patches'].append({'paragraph': 3, 'digest': paragraph_table(s['draft_content'])[2]['digest'],
                               'replacement': '重组', 'finding_ids': ['BIND-0001']})
    with patch.object(rewriter, 'chat', new=AsyncMock(side_effect=[FakeResp(json.dumps(payload)), FakeResp('{}')])):
        out = asyncio.run(rewriter.rewriter_node(s))
    assert '10亿元' in out['draft_content']
    receipt = out['review_history'][-1]['rewrite']
    assert receipt['program_restriction']['outcome'] == 'partial_candidate_applied'
    assert receipt['attempts'][1]['decision'] == 'rejected'
    assert receipt['attempts'][1]['raw_response'] == '{}'
    assert out['forced_output'] is False


def test_no_model_edit_is_not_reported_as_program_restriction():
    s = state()
    s['findings'] = []
    with patch.object(rewriter, 'chat', new=AsyncMock(return_value=FakeResp(
            '{"mode":"patch","patches":[],"results":[]}'))):
        out = asyncio.run(rewriter._rewrite_patch(s, s['draft_content'], [],
                                                 [{'role':'system','content':'返工'}], [], []))
    receipt = out['review_history'][-1]['rewrite']
    assert receipt['program_restriction']['observed'] is False
    assert receipt['program_restriction']['outcome'] == 'no_change_proposed'


def test_restriction_trace_survives_repository_roundtrip():
    from db.database import init_db
    from db import task_repository as repo
    s = state()
    with patch.object(rewriter, 'chat', new=AsyncMock(return_value=FakeResp('{}'))):
        out = asyncio.run(rewriter.rewriter_node(s))
    init_db()
    repo.create_task('restriction', '返工诊断')
    repo.update_task('restriction', review_history=out['review_history'])
    saved = repo.get_task('restriction')['review_history'][-1]['rewrite']
    assert saved['attempts'] == out['review_history'][-1]['rewrite']['attempts']
    assert saved['program_restriction']['outcome'] == 'candidate_blocked'


def test_model_call_failure_is_distinct_from_program_rejection():
    s = state()
    with patch.object(rewriter, 'chat', new=AsyncMock(side_effect=RuntimeError('网络中断'))):
        out = asyncio.run(rewriter.rewriter_node(s))
    audit = out['review_history'][-1]['rewrite']
    assert audit['program_restriction']['observed'] is False
    assert audit['program_restriction']['outcome'] == 'model_call_failed'
    assert all(a['code'] == 'MODEL_CALL_FAILED' for a in audit['attempts'])


def test_full_request_without_local_findings_can_reach_rewriter():
    from agents import reviewer
    draft = '# 报告\n\n## 执行摘要\n摘要\n\n## 正文\n正文\n\n## 结论与建议\n结论'
    with patch.object(reviewer, 'chat', new=AsyncMock(return_value=FakeResp(json.dumps({
        'passed': False, 'comments': ['全篇结构需重组'], 'findings': [], 'full_rewrite_required': True})))):
        out = asyncio.run(reviewer.reviewer_node({'task_id': 't', 'draft_content': draft,
                                                  'evidence': [], 'iteration': 0, 'max_iterations': 3}))
    assert out['forced_output'] is False
    assert out['full_rewrite_required'] is True
    assert out['review_history'][-1]['stop_reason'] is None
