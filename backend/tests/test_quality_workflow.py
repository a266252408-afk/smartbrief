"""使用真实LangGraph/仓储，仅替换模型边界验证质量终态。"""
import asyncio
import pytest
from db import database, task_repository as repo
from graph.workflow import run_task
from tests.fakes import ScriptedLLM


@pytest.mark.parametrize("review,writer,rewrite,execution,quality,forced", [
    ('{"passed":true,"comments":[]}', '## 有效草稿', '## 修改稿', 'completed', 'passed', False),
    ('{"passed":false,"comments":["缺数据"]}', '## 有效草稿', '## 修改稿', 'completed', 'rejected', True),
    ('not json', '## 有效草稿', '## 修改稿', 'completed', 'unavailable', False),
    (RuntimeError('fixture failure'), '## 有效草稿', '## 修改稿', 'completed', 'unavailable', False),
    ('{"passed":"false","comments":[]}', '## 有效草稿', '## 修改稿', 'completed', 'unavailable', False),
    ('{"passed":true,"comments":[]}', '', '## 修改稿', 'failed', 'not_run', False),
    ('{"passed":true,"comments":[]}', RuntimeError('writer failure'), '## 修改稿', 'failed', 'not_run', False),
    ('{"passed":false,"comments":["缺数据"]}', '## 有效草稿', RuntimeError('rewrite failure'), 'failed', 'not_run', False),
    ('{"passed":false,"comments":["缺数据"]}', '## 有效草稿', '', 'failed', 'not_run', False),
])
def test_real_graph_outcomes(tmp_path, monkeypatch, offline_evidence, review, writer, rewrite, execution, quality, forced):
    from agents import planner, react_writer, reviewer, rewriter
    from graph import workflow
    from unittest.mock import AsyncMock
    database.init_db(tmp_path / 'outcome.db')
    repo.create_task('t', '主题')
    plan = '[{"id":1,"agent":"researcher","task":"搜资料","depends_on":[]},{"id":2,"agent":"writer","task":"写报告","depends_on":[1]}]'
    writer = writer + '[S1]' if isinstance(writer,str) and writer.strip() else writer
    rewrite = rewrite + '[S1]' if isinstance(rewrite,str) and rewrite.strip() else rewrite
    for module, responses in [(planner,[plan]),(react_writer,[writer]),(reviewer,[review]*3),(rewriter,[rewrite]*2)]:
        monkeypatch.setattr(module,'chat',ScriptedLLM(responses).chat)
    remember = AsyncMock()
    monkeypatch.setattr(workflow,'_remember_task',remember)
    if execution == 'failed':
        with pytest.raises(RuntimeError):
            asyncio.run(run_task('t','主题'))
    else:
        asyncio.run(run_task('t','主题'))
    task=repo.get_task('t')
    assert task['status'] == task['execution_status'] == execution
    assert task['quality_status'] == quality
    assert task['forced_output'] is forced
    assert bool(task['passed']) is (quality == 'passed')
    assert remember.await_count == int(quality == 'passed')
    assert task['error_code'] is None if quality == 'passed' else bool(task['error_code'])
    assert repo.get_logs('t')
    if quality == 'rejected':
        assert task['forced_pass'] == 1
        assert all(r['passed'] is False for r in task['review_history'])


@pytest.mark.parametrize('raw', ['[]', '{}', 'null', '{"passed":1,"comments":[]}', '{"passed":false,"comments":[]}', '{"passed":true,"comments":[1]}', '{"passed":true,"comments":[],"extra":1}', 'prefix {"passed":true,"comments":[]}'])
def test_review_rejects_unsafe_shapes(raw):
    from agents.reviewer import parse_review
    with pytest.raises(ValueError):
        parse_review(raw)


@pytest.mark.parametrize('draft', [None, '', '   ', '## ', '（报告草稿为空）', '（无 writer 步骤生成内容）', '（章节撰写失败：fixture）', '（步骤 1 撰写超时，内容待补充）'])
def test_placeholder_not_a_report(draft):
    from services.report_quality import usable_draft
    from agents.reviewer import reviewer_node
    from unittest.mock import AsyncMock, patch
    assert not usable_draft(draft)
    with patch('agents.reviewer.chat', new=AsyncMock()) as llm:
        result=asyncio.run(reviewer_node({'draft_content': draft}))
    assert result['quality_status'] == 'not_run'
    assert result['execution_status'] == 'failed'
    llm.assert_not_awaited()


def test_route_terminates_without_lying_about_quality():
    from graph.workflow import route_after_review
    for quality in ('unavailable', 'not_run'):
        assert route_after_review({'quality_status': quality, 'passed': False}) == 'passed'
    assert route_after_review({'quality_status': 'rejected', 'forced_output': True, 'passed': False}) == 'passed'
    assert route_after_review({'quality_status': 'rejected', 'forced_output': False, 'passed': False}) == 'rewrite'
