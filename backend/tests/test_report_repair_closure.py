"""装配、证据覆盖与审核修改清单的跨模块闭环。"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from agents import scheduler, reviewer
from services.citations import evidence_context
from services.evidence_ranking import review_evidence_block
from tests.test_evidence import source
from tests.test_rewriter import FakeResp


def test_final_report_replaces_prior_sections_and_receives_them():
    body = '## 市场分析\n\n市场规模10亿元。[S1]'
    complete = '# 报告\n\n## 执行摘要\n摘要\n\n' + body + '\n\n## 结论与建议\n结论'
    s = {'task_id': 't', 'topic': '市场', 'evidence': [source(excerpt='市场规模10亿元。')],
         'plan': [{'id': 1, 'agent': 'writer', 'task': '写主体章节', 'depends_on': []},
                  {'id': 2, 'agent': 'writer', 'task': '整合主体，输出报告标题、执行摘要、结论与建议', 'depends_on': [1]}]}
    with patch.object(scheduler, 'react_writer_run', new=AsyncMock(side_effect=[body, complete])) as writer:
        out = asyncio.run(scheduler.scheduler_node(s))
    assert body in writer.call_args.kwargs['existing_draft']
    assert out['draft_content'].count('## 市场分析') == 1
    assert out['draft_content'].count('市场规模10亿元') == 1
    assert len(out['draft_sections']) == 2  # 原始章节继续保留以供追溯


def test_all_cited_evidence_and_tail_sentences_reach_review_and_rewrite():
    sources = [source(source_id=f'S{i}', title=f'来源{i}', url=f'https://example.invalid/{i}',
                      excerpt='背景介绍。' * 100 + f'关键指标为{i}亿元。') for i in range(1, 19)]
    ids = [s['source_id'] for s in sources]
    context = json.loads(evidence_context({}, sources, query='市场', required_ids=ids))
    assert set(context['selection']['selected']) >= set(ids)
    assert not context['selection']['missing_required']
    for i, s in enumerate(sources, 1):
        assert any(x['source_id'] == s['source_id'] and f'关键指标为{i}亿元' in x['excerpt'] for x in context['sources'])
    block = review_evidence_block('市场', sources, required_ids=ids)
    for i in range(1, 19):
        assert f'[S{i}]' in block and f'关键指标为{i}亿元' in block


def test_essential_context_overflow_is_explicit_not_silent_drop():
    from services.evidence_ranking import required_evidence_context
    with pytest.raises(ValueError, match='EVIDENCE_CONTEXT_OVERFLOW'):
        required_evidence_context('市场', [source(excerpt='资料' * 1000)], ['S1'], ceiling=500)


def test_duplicate_report_has_executable_global_finding_even_when_model_passes():
    draft = '# 报告\n\n## 执行摘要\n摘要\n\n## 市场分析\n正文\n\n## 市场分析\n重复正文\n\n## 结论与建议\n结论'
    with patch.object(reviewer, 'chat', new=AsyncMock(return_value=FakeResp('{"passed":true,"comments":[]}'))):
        out = asyncio.run(reviewer.reviewer_node({'task_id': 't', 'draft_content': draft, 'evidence': []}))
    structural = [f for f in out['findings'] if f.get('scope') == 'full']
    assert structural and structural[0]['evidence']['kind'] == 'report'
    assert out['full_rewrite_required'] is True
    assert out['quality_status'] == 'rejected'


def test_invalid_model_finding_is_preserved_with_reason():
    payload = {'passed': False, 'comments': ['引用有误'], 'findings': [{
        'finding_id': 'MODEL-1', 'problem': '引用有误', 'expected': '核对', 'severity': 'hard',
        'location': {'paragraph': 1, 'sentence': 1},
        'evidence': {'kind': 'source', 'source_id': 'S1', 'quote': '编造的证据'}}]}
    with patch.object(reviewer, 'chat', new=AsyncMock(return_value=FakeResp(json.dumps(payload)))):
        out = asyncio.run(reviewer.reviewer_node({'task_id': 't', 'draft_content': '正文[S1]', 'evidence': [source()]}))
    rejected = out['review_history'][-1]['rejected_findings']
    assert rejected[0]['finding']['finding_id'] == 'MODEL-1'
    assert rejected[0]['code'] == 'SOURCE_QUOTE_MISMATCH'
