"""task_42c9真实输入与Writer出口的回归，不调用在线模型。"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agents import reviewer, react_writer, scheduler
from services.citations import check_citations
from services.findings import merge_findings
from tests.test_evidence import source
from tests.test_rewriter import FakeResp

SAVED = json.loads((Path(__file__).parent / 'fixtures/reference_audit/task42.json').read_text(encoding='utf-8'))


def test_real_empty_dependency_plan_passes_prior_body_to_final_writer():
    s = dict(SAVED, task_id=SAVED['evidence'][0]['task_id'], topic='智能手表',
             completed_steps=[1, 2, 3], draft_sections={'3': SAVED['draft_sections']['3']})
    with patch.object(scheduler, 'react_writer_run', new=AsyncMock(return_value=SAVED['draft_sections']['4'])) as writer:
        out = asyncio.run(scheduler.scheduler_step_node(s))
    assert writer.call_args.kwargs['existing_draft'] == SAVED['draft_sections']['3']
    assert out['plan'][-1]['depends_on'] == [1, 2, 3]
    assert out['draft_content'].count('## 一、 宏观市场概况') <= 1


def test_plan_normalization_is_idempotent_and_does_not_create_dependency_cycles():
    from services.report_plan import normalize_report_plan
    plan = [{'id': 8, 'agent': 'researcher', 'task': '研究', 'depends_on': [2]},
            {'id': 2, 'agent': 'writer', 'task': '写主体', 'depends_on': []},
            {'id': 1, 'agent': 'writer', 'task': '统稿完整报告', 'depends_on': []}]
    out = normalize_report_plan(plan)
    assert out[1]['depends_on'] == []  # 保留原逆向依赖，不能补成8与2互相等待
    assert out[2]['depends_on'] == [2, 8]
    assert normalize_report_plan(out) == out and plan[2]['depends_on'] == []
    assert scheduler._assemble_sections(out, {'2': '主体', '1': '# 最终报告'}) == '# 最终报告'


def test_explicit_body_only_step_is_not_promoted_to_assembly():
    from services.report_plan import is_assembly_step
    plan = [{'id': 1, 'agent': 'writer', 'task': '写前一章'},
            {'id': 2, 'agent': 'writer', 'task': '撰写完整报告；仅撰写主体章节，不输出报告标题'}]
    assert is_assembly_step(plan[1], plan) is False


def test_legacy_unstructured_problem_history_is_not_convergence_evidence():
    from services.review_progress import review_progress
    result = review_progress({'review_complete': True, 'rewrite': {'changed': True},
                              'hard_error_count': 1, 'hard_finding_set': ['旧版未结构化错误']},
                             [{'rule': 'citation', 'message': '引用错误'}], True)
    assert result['stopped'] is False and result['previous_problem_count'] == 0


def test_deleted_numeric_claims_retire_only_their_required_references():
    from services.citations import citation_lifecycle
    life = citation_lifecycle(SAVED['draft_content'], SAVED['rewritten'], ['S3', 'S4', 'S14', 'S17'])
    assert set(life['retired_ids']) == {'S3', 'S4', 'S14'}
    assert life['required_ids'] == ['S17']
    retained = citation_lifecycle('出货量500万台。[S1]', '出货量500万台。[S2]', ['S1'])
    assert retained['required_ids'] == ['S1']
    qualitative = citation_lifecycle('市场正在转型。[S1]', '市场发生转变。', ['S1'])
    assert qualitative['required_ids'] == ['S1']  # 无法确定语义删除时保守保留


def test_equal_count_different_problems_does_not_stop_rewrite():
    s = {'task_id': 't', 'draft_content': '市场规模99亿元。[S1]',
         'evidence': [source(excerpt='市场规模10亿元。')], 'iteration': 1,
         'review_history': [{'round': 1, 'hard_error_count': 1, 'review_complete': True,
                            'hard_finding_set': [{'rule': 'report_assembly', 'problem': '旧结构问题'}],
                            'rewrite': {'changed': True}}]}
    with patch.object(reviewer, 'chat', new=AsyncMock(return_value=FakeResp('{"passed":true,"comments":[]}'))):
        out = asyncio.run(reviewer.reviewer_node(s))
    assert out['forced_output'] is False
    assert out['review_history'][-1]['convergence_stopped'] is False
    assert out['review_history'][-1]['convergence']['resolved']


def test_incomplete_current_review_never_triggers_convergence():
    s = {'task_id': 't', 'draft_content': '市场规模99亿元。[S1]', 'evidence': [source(excerpt='市场规模10亿元。')]}
    model = {'passed': False, 'comments': ['修引用'], 'findings': [{
        'finding_id': 'MODEL-1', 'problem': '新问题', 'expected': '核对', 'severity': 'hard',
        'location': {'paragraph': 1, 'sentence': 1},
        'evidence': {'kind': 'source', 'source_id': 'S1', 'quote': '不存在的引文'}}]}
    with patch.object(reviewer, 'chat', new=AsyncMock(return_value=FakeResp('{"passed":true,"comments":[]}'))):
        first = asyncio.run(reviewer.reviewer_node(s))
    first['review_history'][-1]['rewrite'] = {'changed': True}
    with patch.object(reviewer, 'chat', new=AsyncMock(return_value=FakeResp(json.dumps(model)))):
        out = asyncio.run(reviewer.reviewer_node(dict(s, iteration=1, review_history=first['review_history'])))
    assert out['review_history'][-1]['review_complete'] is False
    assert out['forced_output'] is False


def test_quarter_and_percentage_point_mismatches_are_actionable():
    from services.claim_semantics import semantic_issues
    sources = [source(title='2026年Q1线上智能手表市场总结', excerpt='Q1三大渠道格局。内容电商零售量份额56.3%。'),
               source(source_id='S2', url='https://example.invalid/2', excerpt='2026年第二季度，中国出货量同比增长7%。')]
    issues = semantic_issues('2026年第三季度内容电商零售量份额56.3%[S1]。\n\n同比增加7个百分点[S2]。', sources)
    assert {i['kind'] for i in issues} == {'period_mismatch', 'percentage_point_mismatch'}
    assert all(i['source_ids'] and i['quote'] and i['expected'] for i in issues)
    assert not semantic_issues('2026年第一季度份额56.3%[S1]。同比增长7%[S2]。', sources)


def test_mixed_period_source_does_not_invent_a_single_quarter():
    from services.claim_semantics import semantic_issues
    evidence = [source(title='2026年市场追踪', excerpt='Q1份额20%，Q3份额30%。')]
    assert not semantic_issues('2026年第三季度份额30%[S1]。', evidence)


def test_geography_and_rate_basis_cannot_be_silently_changed():
    from services.claim_semantics import semantic_issues
    evidence = [source(title='2026年Q1中国智能手表', excerpt='中国出货量同比增长7%。')]
    issues = semantic_issues('2026年第一季度全球出货量环比增长7%[S1]。', evidence)
    assert any(i['kind'] in {'comparison_basis_mismatch', 'geography_mismatch'} for i in issues)
    assert not semantic_issues('2026年第一季度中国出货量同比增长7%[S1]。', evidence)


def test_explicit_source_percentage_points_are_allowed():
    from services.claim_semantics import semantic_issues
    assert not semantic_issues('同比增加7个百分点[S1]。', [source(excerpt='同比增加7个百分点。')])


def test_title_evidence_is_valid_for_quotes_and_numbers():
    evidence = [source(title='2026年预计销量突破7000万台', excerpt='2025年销量6450万台。')]
    check = check_citations('t', '2026年预计销量突破7000万台[S1]。', evidence)
    assert not [i for i in check['binding_issues'] if i['kind'] == 'unbound_number']
    finding = {'finding_id': 'MODEL-1', 'problem': '核对标题预测', 'expected': '保留预测标签', 'severity': 'soft',
               'evidence': {'kind': 'source', 'source_id': 'S1', 'quote': evidence[0]['title']},
               'location': {'paragraph': 1, 'sentence': 1}}
    merged, rejected = merge_findings([], [finding], evidence)
    assert merged and not rejected


def test_writer_fact_table_never_uses_excluded_source():
    evidence = [source(excerpt='市场规模10亿元。'),
                source(source_id='S2', url='https://example.invalid/2', excerpt='市场规模777亿元。')]
    summary = json.dumps({'sources': [evidence[0]], 'selection': {'selected': ['S1'], 'dropped': []}})
    with patch.object(react_writer, '_summarize_research', return_value=summary), \
         patch.object(react_writer, 'chat', new=AsyncMock(return_value=FakeResp('市场规模10亿元。[S1]'))) as writer:
        asyncio.run(react_writer.react_writer_run('写市场章节', 1, [], {}, evidence=evidence))
    user = writer.call_args.args[0][1]['content']
    assert '777' not in user and 'S2' not in user


def test_assembly_receives_every_source_already_cited_in_prior_body():
    sources = [source(source_id=f'S{i}', title=f'来源{i}', url=f'https://example.invalid/{i}',
                      excerpt='行业背景描述。' * 70 + f'市场规模{i}亿元。') for i in range(1, 41)]
    preview = json.loads(react_writer._summarize_research({}, sources, query='统稿完整报告'))
    omitted = sorted({s['source_id'] for s in sources} - {s['source_id'] for s in preview['sources']})
    assert omitted
    sid = omitted[0]
    body = f'## 正文\n\n市场规模{sid[1:]}亿元。[{sid}]'
    complete = '# 报告\n\n## 执行摘要\n摘要\n\n' + body + '\n\n## 结论与建议\n结论'
    with patch.object(react_writer, 'chat', new=AsyncMock(return_value=FakeResp(complete))) as writer:
        asyncio.run(react_writer.react_writer_run('统稿完整报告', 2, [], {}, evidence=sources, existing_draft=body))
    material = writer.call_args.args[0][1]['content'].split('可引用的研究数据：\n', 1)[1]
    context, _ = json.JSONDecoder().raw_decode(material)
    assert sid in {s['source_id'] for s in context['sources']}


def test_fact_rows_inherit_unambiguous_period_and_keep_original_evidence():
    from services.facts import extract_facts
    facts = extract_facts([source(title='2026年Q1中国线上智能手表', excerpt='内容电商零售量份额56.3%。')])
    row = next(f for f in facts if f['value'] == '56.3')
    assert row['year'] == 2026 and row['period'] == 'Q1'
    assert row['geography'] == '中国' and row['text'] in '内容电商零售量份额56.3%。'


def test_wearable_user_population_is_not_relabelled_as_smartwatch_population():
    from services.facts import extract_facts
    rows = extract_facts([source(title='2026年Q1智能手表市场总结',
                                 excerpt='截至2025年末，我国可穿戴设备用户规模3.21亿人。')])
    row = next(f for f in rows if f['value'] == '3.21')
    assert row['statistical_object'] == 'wearable'
    assert row['year'] == 2025 and row['period'] is None


def test_writer_self_check_can_repair_semantic_mismatch():
    evidence = [source(title='2026年Q1线上智能手表', excerpt='内容电商零售量份额56.3%。')]
    with patch.object(react_writer, 'chat', new=AsyncMock(side_effect=[
        FakeResp('2026年第三季度内容电商零售量份额56.3%[S1]。'),
        FakeResp('2026年第一季度内容电商零售量份额56.3%[S1]。')])) as writer:
        content = asyncio.run(react_writer.react_writer_run('写渠道章节', 1, [], {}, evidence=evidence))
    assert '第一季度' in content and writer.await_count == 2


def test_other_period_actual_cannot_fill_annual_forecast_gap():
    s = source(title='2025年全年腕戴设备销量', excerpt='2025年全年销量6450万台。')
    result = check_citations('t', '2026年全年销量预测数据待补充。', [s])
    assert not [i for i in result['binding_issues'] if i['kind'] == 'avoidable_gap']


def test_real_saved_draft_can_use_remaining_rewrite_opportunity_in_graph(monkeypatch):
    from agents import rewriter
    from graph import workflow
    from db import task_repository as repo
    from db.database import init_db
    from services.citations import citation_lifecycle
    init_db()
    tid = SAVED['evidence'][0]['task_id']
    repo.create_task(tid, '智能手表')
    corrected = SAVED['rewritten'].replace('2026年第三季度', '2026年第一季度').replace('7个百分点', '7%')
    async def planner_node(s):
        return {'plan': []}
    async def scheduler_node(s):
        return {'draft_content': SAVED['draft_content'], 'evidence': SAVED['evidence']}
    monkeypatch.setattr(workflow, 'planner_node', planner_node)
    monkeypatch.setattr(workflow, 'scheduler_step_node', scheduler_node)
    monkeypatch.setattr(workflow, 'route_after_step', lambda s: 'reviewer')
    monkeypatch.setattr(workflow, '_remember_task', AsyncMock())
    models = [json.dumps({'passed': False, 'comments': ['删除重复报告并核对事实'], 'full_rewrite_required': True}),
              '{"passed":true,"comments":[]}', '{"passed":true,"comments":[]}']
    monkeypatch.setattr(reviewer, 'chat', AsyncMock(side_effect=[FakeResp(x) for x in models]))
    candidates = iter([SAVED['rewritten'], corrected])
    async def rewrite_response(messages, **kwargs):
        instruction = json.loads(messages[-1]['content'])
        assert instruction['mode'] == 'full'
        return FakeResp(json.dumps({'mode': 'full', 'content': next(candidates), 'reason': '按问题清单核对', 'results': []}))
    # 第二轮为两个局部口径问题，显式模拟局部提案不合法后升级全文的真实程序路径。
    full_chat = AsyncMock(side_effect=rewrite_response)
    async def candidate_response(messages, **kwargs):
        if json.loads(messages[-1]['content'])['mode'] == 'patch':
            return FakeResp('{}')
        return await full_chat(messages, **kwargs)
    monkeypatch.setattr(rewriter, 'chat', AsyncMock(side_effect=candidate_response))
    out = asyncio.run(workflow.run_task(tid, '智能手表'))
    assert out['quality_status'] == 'passed'
    assert len(out['review_history']) == 3 and full_chat.await_count == 2
    second = out['review_history'][1]
    assert second['convergence_stopped'] is False
    assert {'period_mismatch', 'percentage_point_mismatch'} <= {f['rule'] for f in second['findings']}
    assert '返工丢失原引用' not in str(second['comments'])
    saved = repo.get_task(tid)
    assert saved['quality_status'] == 'passed' and saved['review_history'][0]['rewrite']['citation_lifecycle']['retired_ids']
    assert not citation_lifecycle(SAVED['rewritten'], corrected, ['S17'])['retired_ids']
