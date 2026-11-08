"""收敛门禁按硬错误数量判断，保持拒绝与强制输出语义。"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from agents import reviewer
from graph.workflow import route_after_review
from tests.test_evidence import source
from tests.test_rewriter import FakeResp


@pytest.mark.parametrize('previous,stopped', [(1, True), (0, False), (2, False)])
def test_hard_error_count_must_decrease(previous, stopped):
    state = {'task_id': 't', 'draft_content': '市场规模99亿元。[S1]',
             'evidence': [source(excerpt='市场规模10亿元。')], 'iteration': 0}
    with patch.object(reviewer, 'chat', new=AsyncMock(return_value=FakeResp('{"passed":true,"comments":[]}'))):
        first = asyncio.run(reviewer.reviewer_node(state))
    old = first['review_history'][0]['hard_finding_set'] if previous else []
    if previous == 2:
        old = old + [{'rule': 'report_assembly', 'problem': '旧结构问题'}]
    state.update(iteration=1, review_history=[{'round': 1, 'hard_error_count': previous,
                 'hard_finding_set': old, 'review_complete': True, 'rewrite': {'changed': True}}])
    with patch.object(reviewer, 'chat', new=AsyncMock(return_value=FakeResp('{"passed":true,"comments":[]}'))):
        out = asyncio.run(reviewer.reviewer_node(state))
    assert out['forced_output'] is stopped
    assert out['quality_status'] == 'rejected'
    assert out['review_history'][-1]['hard_error_count'] == 1
    assert bool(out['review_history'][-1]['convergence_stopped']) is stopped
    assert (route_after_review(out) == 'passed') is stopped
