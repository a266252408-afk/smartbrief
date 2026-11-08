"""R01-01：冻结标准和真实审核边界的质量判定，仅替换模型边界。"""
import asyncio
import json
from unittest.mock import patch
import pytest
from agents import reviewer
from services import review_policy
from tests.fakes import ScriptedLLM
from tests.test_evidence import source

BODY = '# 研究报告\n\n## 执行摘要\n\n市场规模10亿元，增长20%[S1]。\n\n## 市场数据\n\n资料说明：研究范围有限。\n\n## 结论与建议\n\n建议进一步核验。'


def state(body=BODY):
    return dict(task_id='t', topic='行业调查', report_pipeline_version=2, draft_content=body,
                evidence=[source(excerpt='市场规模10亿元，增长20%。')])


def test_frozen_policy_is_reused_and_task_isolated():
    policy = review_policy.freeze_policy(state())
    assert policy['required_questions'] == ['行业调查']
    assert policy['data_requirements']['minimum_points'] == 2
    assert review_policy.freeze_policy(dict(state(), topic='不得临时增加的问题', review_policy=policy)) == policy
    with pytest.raises(ValueError):
        review_policy.freeze_policy(dict(state(), task_id='other', review_policy=policy))


def test_policy_contract_rejects_extra_fields_and_lowered_data_requirement():
    policy = review_policy.freeze_policy(state())
    for invalid in (dict(policy, hidden_preference='每章小结'),
                    dict(policy, data_requirements=dict(policy['data_requirements'], minimum_points=0))):
        with pytest.raises(ValueError):
            review_policy.freeze_policy(dict(state(), review_policy=invalid))


@pytest.mark.parametrize('usable,complete,blockers,unresolved,expected', [
    (False,False,[],False,'not_run'), (True,True,[],False,'passed'),
    (True,False,[],False,'unavailable'), (True,True,[],True,'unavailable'),
    (True,False,['invalid_reference'],False,'rejected'),
    (True,True,['fact_conflict'],False,'rejected')])
def test_final_quality_truth_table(usable, complete, blockers, unresolved, expected):
    result = review_policy.decide_quality(usable=usable, review_complete=complete,
                                         confirmed_blockers=blockers, unresolved_material=unresolved)
    assert result['quality_status'] == expected
    assert result['passed'] == (expected == 'passed')
    assert result['review_complete'] == complete


def soft_response(passed):
    return json.dumps(dict(passed=passed, comments=[] if passed else ['建议简化结尾措辞'], findings=[dict(
        finding_id='MODEL-0001', problem='结尾措辞可更简洁', expected='精简语句', severity='soft',
        evidence=dict(kind='report',quote='建议进一步核验。'),location=dict(paragraph=7,sentence=1))]),ensure_ascii=False)


def test_soft_only_boolean_conflict_corrects_review_once_without_dropping_advice():
    model = ScriptedLLM([soft_response(False), soft_response(True)])
    with patch.object(reviewer, 'chat', new=model.chat):
        out = asyncio.run(reviewer.reviewer_node(state()))
    assert out['quality_status'] == 'passed'
    assert len(model.calls) == 2
    assert out['review_history'][-1]['findings'][0]['severity'] == 'soft'
    assert out['review_history'][-1]['policy_corrections']


def test_unresolved_boolean_conflict_is_unavailable_not_accepted():
    model = ScriptedLLM([soft_response(False), soft_response(False)])
    with patch.object(reviewer, 'chat', new=model.chat):
        out = asyncio.run(reviewer.reviewer_node(state()))
    assert out['quality_status'] == 'unavailable'
    assert len(model.calls) == 2


def test_review_failure_keeps_confirmed_reference_rejection_and_incomplete_flag():
    model = ScriptedLLM(['不是JSON'])
    with patch.object(reviewer, 'chat', new=model.chat):
        out = asyncio.run(reviewer.reviewer_node(state(BODY+'\n\n另见[S999]。')))
    assert out['quality_status'] == 'rejected'
    assert out['review_history'][-1]['review_complete'] is False
    assert out['review_history'][-1]['review_diagnostics']


def test_review_failure_without_confirmed_issue_is_unavailable():
    model = ScriptedLLM(['不是JSON'])
    with patch.object(reviewer, 'chat', new=model.chat):
        out = asyncio.run(reviewer.reviewer_node(state()))
    assert out['quality_status'] == 'unavailable'


def test_policy_is_immutable_and_decision_does_not_coerce_booleans():
    policy = review_policy.ReviewPolicy.model_validate_json(json.dumps(review_policy.freeze_policy(state())))
    with pytest.raises(ValueError):
        policy.task_id = 'other'
    with pytest.raises(ValueError):
        policy.data_requirements.minimum_points = 1
    with pytest.raises(ValueError):
        review_policy.decide_quality(usable='true', review_complete=True, confirmed_blockers=[])
    assert review_policy.freeze_policy(dict(state(), topic=''))['required_questions'] == []


def test_rewriter_receives_the_same_frozen_policy():
    from agents import rewriter
    from services.fact_binding import build_registry
    from tests.test_rewriter_patch_flow import state as rewrite_state, response
    from unittest.mock import AsyncMock
    task = rewrite_state()
    task.update(topic='行业调查', report_pipeline_version=2, review_policy=review_policy.freeze_policy(state()))
    key = next(iter(build_registry('t', task['evidence'])))
    model = AsyncMock(return_value=response(task, f'市场规模10亿元[{key}]。'))
    with patch.object(rewriter, 'chat', new=model):
        result = asyncio.run(rewriter.rewriter_node(task))
    assert '99亿元' not in result['draft_content']
    for call in model.call_args_list:
        assert review_policy.policy_text(task['review_policy']) in '\n'.join(m['content'] for m in call.args[0])


def test_v2_resume_reuses_frozen_policy_after_writer_interruption(offline_evidence, monkeypatch):
    import re
    from unittest.mock import AsyncMock
    from agents import react_researcher
    from db import database, task_repository
    from graph.workflow import run_task, build_graph
    from memory.checkpointer import open_checkpointer
    from services.usage_service import UsagePersistenceError
    database.init_db()
    task_repository.create_task('policy-resume', '市场研究', metadata={'report_pipeline_version': 2})
    monkeypatch.setattr(react_researcher.mcp_manager, 'call_tool', AsyncMock(return_value={
        'source': 'fixture', 'results': [{'title': '季度统计', 'url': 'https://example.invalid/policy',
        'snippet': '2026年第一季度市场规模10亿元。同比增长20%。'}]}))
    offline_evidence['planner'].outputs = [json.dumps([
        dict(id=1, agent='researcher', task='研究市场', depends_on=[]),
        dict(id=2, agent='writer', task='完整报告', depends_on=[1])])]
    def write(messages):
        ids = re.findall(r'^\[(F[a-f0-9]+)\]', messages[-1]['content'], re.M)
        return ('# 市场研究报告\n\n## 执行摘要\n推断：建议关注变化。\n\n## 正文\n'
                f'2026年第一季度市场规模10亿元[{ids[0]}]。\n\n同比增长20%[{ids[1]}]。\n\n'
                '## 结论与建议\n建议跟踪后续统计。')
    offline_evidence['react_writer'].outputs = [UsagePersistenceError('离线中断'), write]
    offline_evidence['reviewer'].outputs = ['{"passed":true,"comments":[]}']
    async def scenario():
        with pytest.raises(UsagePersistenceError):
            await run_task('policy-resume', '市场研究')
        async with open_checkpointer() as (cp, kind):
            assert kind == 'sqlite'
            saved = await build_graph(checkpointer=cp).aget_state({'configurable': {'thread_id': 'policy-resume'}})
            frozen = saved.values['review_policy']
            assert frozen['required_questions'] == ['市场研究']
        research_calls = len(offline_evidence['react_researcher'].calls)
        from services.task_service import TaskService
        service = TaskService()
        try:
            accepted = await service.resume('policy-resume', key='policy-resume-action')
            assert accepted['resume_mode'] == 'checkpoint'
            task = await service.wait('policy-resume')
        finally:
            await service.close()
        assert task['quality_status'] == 'passed'
        assert task['review_history'][-1]['review_policy'] == frozen
        assert len(offline_evidence['planner'].calls) == 1
        assert len(offline_evidence['react_researcher'].calls) == research_calls
    asyncio.run(scenario())


def test_dispute_request_inherits_frozen_policy():
    initial = dict(passed=False, comments=['核对数字'], findings=[dict(
        finding_id='MODEL-0001', problem='数值疑问', expected='对照来源核验', severity='hard',
        evidence=dict(kind='source',source_id='S1',quote='市场规模10亿元，增长20%。'),
        location=dict(paragraph=3,sentence=1))])
    model = ScriptedLLM([json.dumps(initial, ensure_ascii=False), '{"passed":true,"comments":[]}'])
    with patch.object(reviewer, 'chat', new=model.chat):
        result = asyncio.run(reviewer.reviewer_node(state()))
    assert result['quality_status'] == 'passed'
    assert len(model.calls) == 2
    assert review_policy.policy_text(review_policy.freeze_policy(state())) in model.calls[1][0][0]['content']


def test_passed_true_with_own_hard_finding_is_also_corrected_once():
    finding = dict(finding_id='MODEL-0001', problem='核心问题分析不足', expected='补充有证据的分析',
        severity='hard', evidence=dict(kind='report',quote='资料说明：研究范围有限。'),
        location=dict(paragraph=5,sentence=1))
    model = ScriptedLLM([json.dumps(dict(passed=value, comments=['需要分析'], findings=[finding]), ensure_ascii=False)
                         for value in (True, False)])
    with patch.object(reviewer, 'chat', new=model.chat):
        result = asyncio.run(reviewer.reviewer_node(state()))
    assert result['quality_status'] == 'rejected'
    assert len(model.calls) == 2
    assert result['review_history'][-1]['policy_corrections']
