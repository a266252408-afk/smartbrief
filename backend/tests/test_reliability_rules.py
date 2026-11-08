"""R01-02规则正反例：程序未知覆盖不能拒绝为事实错误，也不能放行。"""
import asyncio
import json
from unittest.mock import patch
import pytest
from agents import reviewer
from services.citations import check_citations
from services.fact_binding import BindingError, render_bound_text
from services.findings import build_findings
from services.report_quality import structure_gaps
from tests.fakes import ScriptedLLM
from tests.test_evidence import source


@pytest.mark.parametrize('text', [
    '下文将从需求、竞争和风险三个方面展开分析。',
    '以下将从市场规模、需求和竞争三个方面进行分析。',
    '接下来将依次讨论需求、竞争与风险。'])
def test_full_organizational_sentence_needs_no_fact_id(text):
    assert render_bound_text(text, 't', {})[0] == text


@pytest.mark.parametrize('text', ['下文将从预计增长20%、竞争和风险三个方面展开分析。',
                                 '建议关注该品牌排名第二的优势。'])
def test_transition_or_advice_does_not_exempt_embedded_fact(text):
    with pytest.raises(BindingError):
        render_bound_text(text, 't', {})


def test_equivalent_headings_do_not_make_empty_sections_valid():
    gaps = structure_gaps('# 报告\n\n## 执行摘要\n\n## 正文\n\n## 结论与建议')
    assert any('summary' in g['kind'] for g in gaps)
    assert any('conclusion' in g['kind'] for g in gaps)


def test_money_unit_omission_is_normalized_in_explicit_money_context():
    evidence = [source(excerpt='行业资料介绍。企业客单价稳定在8万以上，年营收超3000万。')]
    check = check_citations('t', '企业客单价稳定在8万元以上[S1]。', evidence)
    assert not any(i['kind'] == 'unsupported_wording' for i in check['binding_issues'])


@pytest.mark.parametrize('excerpt', ['企业拥有8万以上的用户。', '企业客单价稳定在8万美元以上。',
                                    '企业拥有8万以上的用户，客单价为5000元。'])
def test_money_normalization_does_not_invent_units_or_currency(excerpt):
    check = check_citations('t', '企业客单价稳定在8万元以上[S1]。', [source(excerpt=excerpt)])
    assert any(i['kind'] == 'unsupported_wording' for i in check['binding_issues'])


def test_threshold_finding_points_to_relevant_source_sentence():
    evidence = [source(excerpt='这是来源的行业介绍。企业客单价稳定在8万以上，年营收超3000万。')]
    body = '企业客单价稳定在9万元以上[S1]。'
    findings = build_findings(body, evidence, check_citations('t', body, evidence)['binding_issues'])
    wording = next(f for f in findings if f['rule'] == 'unsupported_wording')
    assert '客单价' in wording['evidence']['quote']


def report(sentence):
    return '# 行业报告\n\n## 执行摘要\n\n推断：需要进一步核验。\n\n## 市场数据\n\n'+sentence+'\n\n## 结论与建议\n\n建议跟踪资料。'


def run_review(body, evidence, output=None):
    model = ScriptedLLM([output or '{"passed":true,"comments":[]}'])
    with patch.object(reviewer, 'chat', new=model.chat):
        result = asyncio.run(reviewer.reviewer_node(dict(task_id='t', topic='行业调查', report_pipeline_version=2,
                                                       draft_content=body, evidence=evidence)))
    return result


def test_comparison_suspicion_is_neither_confirmed_rejection_nor_auto_pass():
    body = report('乙品牌市场规模10亿元，仅次于甲品牌[S1]。')
    result = run_review(body, [source(excerpt='乙品牌市场规模10亿元。')])
    assert result['quality_status'] == 'unavailable'
    assert result['review_history'][-1]['review_complete'] is False
    finding = next(f for f in result['findings'] if f['rule'] == 'unverified_comparison')
    assert finding['decision'] == 'suspected'


def test_explicit_period_conflict_remains_a_blocking_error():
    result = run_review(report('2025年市场规模10亿元[S1]。'), [source(excerpt='2024年市场规模10亿元。')])
    assert result['quality_status'] == 'rejected'
    assert any(f.get('decision') == 'confirmed' for f in result['findings'] if f.get('rule') == 'period_mismatch')


@pytest.mark.parametrize('title,excerpt', [('2024年发布', '市场规模10亿元。'), ('2024年度资料', '市场规模10亿元。'),
    ('来源', '发布日期：2024年，市场规模10亿元。')])
def test_publication_year_is_not_confirmed_as_measurement_year(title,excerpt):
    result = run_review(report('2025年市场规模10亿元[S1]。'), [source(title=title, excerpt=excerpt)])
    assert result['quality_status'] == 'unavailable'


def test_source_soft_finding_is_not_assumed_to_be_nonblocking_style():
    body = report('市场规模10亿元[S1]。')
    output = json.dumps(dict(passed=True,comments=[],findings=[dict(finding_id='ATTR-1',problem='来源归因需核验',
        expected='核对完整原文',severity='soft',evidence=dict(kind='source',source_id='S1',quote='市场规模10亿元。'),
        location=dict(paragraph=5,sentence=1))]),ensure_ascii=False)
    result = run_review(body,[source(excerpt='市场规模10亿元。')],output)
    assert result['quality_status'] == 'unavailable'


def test_rule_classification_distinguishes_exact_conflict_unknown_and_information():
    from services.review_rules import assess_issue, is_confirmed_blocker, material_pending
    evidence = [source(excerpt='市场规模10亿元。')]
    confirmed = assess_issue(dict(kind='unbound_number',severity='hard',sentence='市场规模99亿元。',source_ids=['S1']), evidence)
    unknown = assess_issue(dict(kind='unbound_number',severity='hard',sentence='另一个行业规模99亿元。',source_ids=['S1']), evidence)
    info = assess_issue(dict(kind='unbound_source',severity='info'), evidence)
    assert is_confirmed_blocker(confirmed)
    assert not is_confirmed_blocker(unknown) and material_pending(unknown)
    assert info['decision'] == 'dismissed' and not material_pending(info)
    missing = assess_issue(dict(kind='new_unknown_rule',severity='hard'), [])
    assert material_pending(missing) and not is_confirmed_blocker(missing)


def test_money_finding_selects_related_source_in_group():
    evidence = [source(excerpt='行业整体介绍。'), source(source_id='S2', url='https://example.invalid/money', excerpt='企业客单价稳定在8万以上。')]
    body = '企业客单价稳定在9万元以上[S1][S2]。'
    findings = build_findings(body,evidence,check_citations('t',body,evidence)['binding_issues'])
    finding = next(f for f in findings if f['rule']=='unsupported_wording')
    assert finding['evidence']['source_id'] == 'S2'
    assert '客单价' in finding['evidence']['quote']


def test_unknown_heading_semantics_requires_verification_not_false_rejection():
    body = '# 行业报告\n\n## 研究发现\n\n市场规模10亿元[S1]。\n\n## 展望\n\n建议持续跟踪。'
    result = run_review(body,[source(excerpt='市场规模10亿元。')])
    assert result['quality_status'] == 'unavailable'
    assert any(f.get('decision') == 'suspected' for f in result['findings'])


@pytest.mark.parametrize('kind,sentence,quote,decision', [
    ('geography_mismatch','中国市场规模10亿元。','全球市场规模10亿元。','confirmed'),
    ('geography_mismatch','中国市场规模10亿元。','市场规模10亿元。','suspected'),
    ('comparison_basis_mismatch','市场环比增长10%。','市场同比增长10%。','confirmed'),
    ('comparison_basis_mismatch','市场环比增长10%。','市场增长10%。','suspected')])
def test_scope_conflicts_need_explicit_source_fields(kind,sentence,quote,decision):
    from services.review_rules import assess_issue
    issue = assess_issue(dict(kind=kind,severity='hard',sentence=sentence,quote=quote,source_ids=['S1']),[source(excerpt=quote)])
    assert issue['decision'] == decision


def test_numeric_assessment_retains_unknown_tail_and_dismisses_matching_value():
    from services.review_rules import assess_issue, material_pending
    issue = dict(kind='unbound_number',severity='hard',sentence='市场规模10亿元。',source_ids=['S1'])
    assert assess_issue(issue,[source(excerpt='市场规模10亿元。')])['decision'] == 'dismissed'
    incomplete = assess_issue(issue,[source(excerpt='市场规模10亿元。',excerpt_status='incomplete_tail')])
    assert material_pending(incomplete)
