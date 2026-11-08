"""返工补丁应用、校验、回滚与降级。"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

from agents import rewriter
from services.rewrite_patches import paragraph_table
from tests.test_rewriter import FakeResp
from tests.test_evidence import source


def state():
    return {'task_id': 't', 'draft_content': '# 标题\n\n市场规模99亿元。[S1]\n\n保留原文  ',
            'evidence': [source('t', excerpt='市场规模10亿元。')],
            'findings': [{'finding_id': 'BIND-0001', 'rule': 'unbound_number', 'severity': 'hard',
                          'location': {'paragraph': 2}, 'numbers': ['99亿元']}], 'review_comments': []}


def response(s, replacement):
    return FakeResp(json.dumps({'mode': 'patch', 'patches': [dict({k:v for k,v in paragraph_table(s['draft_content'])[1].items() if k != 'text'},
        replacement=replacement, finding_ids=['BIND-0001'])],
        'results': [{'finding_id': 'BIND-0001', 'status': 'fixed'}]}, ensure_ascii=False))


def test_patch_flow_keeps_other_paragraphs():
    s = state()
    with patch.object(rewriter, 'chat', new=AsyncMock(return_value=response(s, '市场规模10亿元。[S1]'))) as chat:
        out = asyncio.run(rewriter.rewriter_node(s))
    assert chat.await_count == 1
    assert out['draft_content'] == s['draft_content'].replace('99亿元', '10亿元')
    assert out['review_history'][-1]['rewrite']['mode'] == 'patch'


def test_unresolved_patch_rolls_back_retries_then_full():
    s = state()
    full = FakeResp(json.dumps({'mode': 'full', 'reason': '补丁连续失败', 'content': '# 标题\n\n市场规模10亿元。[S1]',
                               'results': [{'finding_id': 'BIND-0001', 'status': 'fixed'}]}))
    with patch.object(rewriter, 'chat', new=AsyncMock(side_effect=[response(s, '市场规模99亿元。[S1]')]*2 + [full])) as chat:
        out = asyncio.run(rewriter.rewriter_node(s))
    assert chat.await_count == 3
    receipt = out['review_history'][-1]['rewrite']
    assert receipt['mode'] == 'full'
    assert len(receipt['failures']) == 2
    assert '99亿元' not in out['draft_content']


def test_all_attempts_fail_preserves_original_and_persists_receipts():
    from db import task_repository as repo
    s = state()
    s['review_history'] = [{'round': 1, 'findings': s['findings']}]
    with patch.object(rewriter, 'chat', new=AsyncMock(return_value=FakeResp('{}'))):
        out = asyncio.run(rewriter.rewriter_node(s))
    assert out['draft_content'] == s['draft_content']
    assert out['review_history'][-1]['rewrite']['mode'] == 'rollback'
    assert out['review_history'][-1]['rewrite']['results'][0]['status'] == 'cannot_fix'
    assert 'rewrite' not in s['review_history'][0]
    from db.database import init_db
    init_db()
    repo.create_task('patch-persist', '补丁持久化')
    repo.update_task('patch-persist', review_history=out['review_history'])
    assert repo.get_task('patch-persist')['review_history'] == out['review_history']


def test_digest_mismatch_retries_against_original():
    s = state()
    wrong = json.loads(response(s, '市场规模10亿元。[S1]').choices[0].message.content)
    wrong['patches'][0]['digest'] = 'sha256:wrong'
    with patch.object(rewriter, 'chat', new=AsyncMock(side_effect=[FakeResp(json.dumps(wrong)), response(s, '市场规模10亿元。[S1]')])) as chat:
        out = asyncio.run(rewriter.rewriter_node(s))
    assert chat.await_count == 2
    assert out['draft_content'].endswith('保留原文  ')
    assert 'digest' in out['review_history'][-1]['rewrite']['failures'][0]


def test_soft_finding_can_be_skipped_with_reason():
    s = state()
    s['draft_content'] = '市场规模10亿元。[S1]'
    s['findings'][0].update(severity='soft', rule='reviewer', location={'paragraph': 1})
    payload = {'mode': 'patch', 'patches': [], 'results': [
        {'finding_id': 'BIND-0001', 'status': 'skipped', 'reason': '资料不足'}]}
    with patch.object(rewriter, 'chat', new=AsyncMock(return_value=FakeResp(json.dumps(payload)))):
        out = asyncio.run(rewriter.rewriter_node(s))
    assert out['draft_content'] == s['draft_content']
    assert out['review_history'][-1]['rewrite']['results'][0]['reason'] == '资料不足'


def test_structurally_complete_report_defaults_to_patch_without_findings():
    s = state()
    s['draft_content'] = '# 标题\n\n## 执行摘要\n摘要。[S1]\n\n## 正文\n正文。[S1]\n\n## 结论与建议\n结论。[S1]'
    s['findings'] = []
    with patch.object(rewriter, 'chat', new=AsyncMock(return_value=FakeResp('{"mode":"patch","patches":[],"results":[]}'))) as chat:
        out = asyncio.run(rewriter.rewriter_node(s))
    assert out['draft_content'] == s['draft_content']
    assert out['review_history'][-1]['rewrite']['mode'] == 'patch'
    assert 'paragraphs' in chat.call_args.args[0][-1]['content']
