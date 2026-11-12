"""R01-02规则正反例：程序未知覆盖不能拒绝为事实错误，也不能放行。"""
import asyncio
import json
import re
from unittest.mock import patch
import pytest
from agents import reviewer
from services.citations import check_citations
from services.fact_binding import BindingError, render_bound_text
from services.findings import build_findings
from services.report_quality import structure_gaps, repair_structure
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
    # R04裁决未能核验时记为unresolved，与suspected同为非确认待核验状态。
    assert finding['decision'] in {'suspected', 'unresolved'}


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
    assert any(f.get('decision') in {'suspected', 'unresolved'} for f in result['findings'])


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


def test_glued_chapter_heading_is_split_from_body_sentence():
    body = ('# 2026年汽车行业市场调研报告\n\n## 执行摘要\n\n摘要内容[S1]。\n\n'
            '## 出口表现与二手车生态 “国内冷、海外热”成为核心标签，出口填补缺口。[S36]\n\n'
            '## 结论与建议\n\n建议内容[S1]。')
    out = repair_structure(body)
    assert '## 出口表现与二手车生态\n\n“国内冷、海外热”成为核心标签，出口填补缺口。[S36]' in out
    assert '二手车生态 “' not in out


def test_glued_required_heading_is_split_without_rewriting_words():
    body = ('# 报告\n\n## 执行摘要\n\n摘要内容[S1]。\n\n'
            '## 结论与建议行业逻辑已切换至价值竞争[S7]。B端保持稳健。')
    out = repair_structure(body)
    assert '## 结论与建议\n\n行业逻辑已切换至价值竞争[S7]。B端保持稳健。' in out


def test_numbered_glued_headings_are_split_without_rewriting_words():
    body = ('# 报告\n\n## 执行摘要\n\n摘要内容[S1]。\n\n'
            '## 二、产能扩张与产业链重构中泰证券统计显示，2026年上半年成本下降80%至90%[S14]。\n\n'
            '## 三、用户规模与流量生态据调研机构QuestMobile数据，2026年2月用户达7.18亿[S2]。\n\n'
            '## 四、盈利瓶颈与同质化困境DataEye报告显示，仅1.3%新剧没有亏钱[S14]。\n\n'
            '## 五、监管规范与分级治理《微短剧发展管理办法》将自2026年9月1日起施行[S8]。\n\n'
            '## 结论与建议\n\n建议内容[S1]。')
    out = repair_structure(body)
    assert '## 二、产能扩张与产业链重构\n\n中泰证券统计显示，2026年上半年成本下降80%至90%[S14]。' in out
    assert '## 三、用户规模与流量生态\n\n据调研机构QuestMobile数据，2026年2月用户达7.18亿[S2]。' in out
    assert '## 四、盈利瓶颈与同质化困境\n\nDataEye报告显示，仅1.3%新剧没有亏钱[S14]。' in out
    assert '## 五、监管规范与分级治理\n\n《微短剧发展管理办法》将自2026年9月1日起施行[S8]。' in out
    assert re.sub(r'\s+', '', out) == re.sub(r'\s+', '', body)
    assert structure_gaps(out) == []
    assert repair_structure(out) == out


def test_un_numbered_glued_headings_with_weak_cues_are_split():
    body = ('# 报告\n\n## 执行摘要\n\n摘要内容[S1]。\n\n'
            '## 供给侧动态与成本重构追溯发展轨迹，2025年以前供给不足千部[S24]。\n\n'
            '## 盈利分化与竞争格局当前主流盈利路径依赖投流、分账与版权交易[S4]。\n\n'
            '### 监管环境与行业洗牌今年二季度以来，模型算力成本上升[S13]。\n\n'
            '## 结论与建议\n\n建议内容[S1]。')
    out = repair_structure(body)
    assert '## 供给侧动态与成本重构\n\n追溯发展轨迹，2025年以前供给不足千部[S24]。' in out
    assert '## 盈利分化与竞争格局\n\n当前主流盈利路径依赖投流、分账与版权交易[S4]。' in out
    assert '### 监管环境与行业洗牌\n\n今年二季度以来，模型算力成本上升[S13]。' in out
    assert re.sub(r'\s+', '', out) == re.sub(r'\s+', '', body)
    assert structure_gaps(out) == []


def test_glued_headings_without_body_cues_split_on_title_tail():
    body = ('# 报告\n\n## 执行摘要\n\n摘要内容[S1]。\n\n'
            '## 成本重构与盈利困境羊阳给记者算了一笔账：成本约20万元[S2]。\n\n'
            '## 监管规范与出海动态国家广播电视总局发布《管理提示》[S33]。\n\n'
            '## 市场规模与增长预期其中AI剧及漫剧细分赛道表现强劲[S2]。\n\n'
            '## 市场规模与增长轨迹当年同比增速达到33.8%[S5]。\n\n'
            '## 结论与建议\n\n建议内容[S1]。')
    out = repair_structure(body)
    assert '## 成本重构与盈利困境\n\n羊阳给记者算了一笔账：成本约20万元[S2]。' in out
    assert '## 监管规范与出海动态\n\n国家广播电视总局发布《管理提示》[S33]。' in out
    assert '## 市场规模与增长预期\n\n其中AI剧及漫剧细分赛道表现强劲[S2]。' in out
    assert '## 市场规模与增长轨迹\n\n当年同比增速达到33.8%[S5]。' in out
    assert re.sub(r'\s+', '', out) == re.sub(r'\s+', '', body)
    assert structure_gaps(out) == []


def test_glued_headings_with_uncommon_title_tails_are_split():
    body = ('# 报告\n\n## 执行摘要\n\n摘要内容[S1]。\n\n'
            '## 政策环境与标准建设自2010年启动试点以来，政策体系逐步完善。[S4]\n\n'
            '## 典型应用场景实证福建漳州高校低空配送航线单次飞行距离5.5公里。[S26]\n\n'
            '## 结论与建议\n\n建议内容[S1]。')
    out = repair_structure(body)
    assert '## 政策环境与标准建设\n\n自2010年启动试点以来，政策体系逐步完善。[S4]' in out
    assert '## 典型应用场景实证\n\n福建漳州高校低空配送航线单次飞行距离5.5公里。[S26]' in out
    assert structure_gaps(out) == []


def test_unbound_number_confirms_miscitation_when_candidate_source_has_value():
    from services.review_rules import assess_issue, is_confirmed_blocker
    issue = dict(kind='unbound_number', rule='unbound_number', severity='hard',
                 sentence='五年期间相关市场主体年均复合增长率达到13.2%[S10]。',
                 cited=['S10'], candidate_sources=['S9'], numbers=['13.2%'])
    sources = [source(source_id='S10', excerpt='低空经济企业数量增长。'),
               source(source_id='S9', excerpt='截至2025年，全国低空经济企业数量已达7.61万家，五年年均复合增长率13.2%。')]
    assessed = assess_issue(issue, sources)
    assert assessed['decision'] == 'confirmed'
    assert is_confirmed_blocker(assessed)


def test_model_comparison_finding_without_comparison_quote_is_dismissed():
    from services.review_rules import assess_model_finding, is_confirmed_blocker
    finding = dict(finding_id='MODEL-0001', severity='hard',
                   problem='比较级声明与来源排序不一致',
                   expected='来源未明确给出“增速达到年内最高水平”的对比范围',
                   evidence={'kind': 'report', 'quote': '同比增速达到12.6% [S4]。'})
    assessed = assess_model_finding(finding)
    assert assessed['decision'] == 'dismissed'
    assert not is_confirmed_blocker(assessed)


def test_model_finding_numbers_absent_from_report_quote_is_dismissed():
    from services.review_rules import assess_model_finding, is_confirmed_blocker
    finding = dict(finding_id='MODEL-0001', severity='hard',
                   problem='报告称2025年中国咖啡产业整体规模已达3549亿元，引用[S13]不支持。',
                   expected='改为[S1]',
                   evidence={'kind': 'report', 'quote': '### 市场基本面与规模增长\n咖啡产业盘子保持稳步扩张节奏。[S13]'})
    assessed = assess_model_finding(finding)
    assert assessed['decision'] == 'dismissed'
    assert not is_confirmed_blocker(assessed)


def test_model_finding_numbers_present_in_quote_is_kept():
    from services.review_rules import assess_model_finding, is_confirmed_blocker
    finding = dict(finding_id='MODEL-0002', severity='hard',
                   problem='数字400亿元不在所引来源',
                   expected='改为[S2]',
                   evidence={'kind': 'report', 'quote': '市场规模将达400亿元。[S1]'})
    assessed = assess_model_finding(finding)
    assert assessed['decision'] == 'confirmed'
    assert is_confirmed_blocker(assessed)


def test_source_model_finding_numbers_absent_from_report_sentence_is_dismissed():
    from services.review_rules import assess_model_finding, is_confirmed_blocker
    finding = dict(finding_id='MODEL-0001', severity='hard',
                   problem='引用归因错误：库迪供应链基地被放进瑞幸上下文',
                   expected='将4.5万吨产能明确标注为库迪数据',
                   evidence={'kind': 'source', 'source_id': 'S26',
                             'quote': '库迪全球供应链基地年产能4.5万吨'})
    assessed = assess_model_finding(finding, report_sentence='瑞幸已加强与巴西等原材料产地合作[S26]。')
    assert assessed['decision'] == 'dismissed'
    assert not is_confirmed_blocker(assessed)


def test_citation_group_is_split_by_numeric_source():
    from services.citations import repair_citation_groups
    body = ('伴随不同基数，增长率口径亦存在区分：上述突破性规模来源指出未来五年预计以年均复合增长率约12%稳健扩张，'
            '而长周期统计口径显示2014-2024年年均复合增长率为21.4%，另有分析模型预测2024-2030年年复合增长率为3.9%。'
            '[S1][S3][S13]')
    sources = [source(source_id='S1', excerpt='截至2024年底，市场规模已突破1500亿元，未来五年内继续以年均复合增长率约12%的速度稳健扩张。'),
               source(source_id='S3', excerpt='2014-2024年年均复合增长率为21.4%。'),
               source(source_id='S13', excerpt='2024-2030年年复合增长率为3.9%。')]
    out, fixed = repair_citation_groups(body, sources)
    assert fixed == 1
    assert '约12%稳健扩张[S1]' in out
    assert '21.4%[S3]' in out
    assert '3.9%[S13]' in out
    assert '[S1][S3][S13]' not in out


def test_embedded_second_report_title_is_removed_without_losing_body():
    body = ('# 2026年中国AI漫剧市场调研报告\n\n## 执行摘要\n\n摘要内容[S1]。\n\n'
            '## 2026年中国AI漫剧市场调研报告\n\n## 正文\n\n正文内容[S1]。\n\n'
            '## 结论与建议\n\n建议内容[S1]。')
    out = repair_structure(body)
    assert out.count('2026年中国AI漫剧市场调研报告') == 1
    assert '## 正文\n\n正文内容[S1]。' in out
    assert structure_gaps(out) == []


def test_leading_citation_after_glued_heading_is_moved_to_sentence_end():
    body = ('# 报告\n\n## 执行摘要\n\n摘要[S1]。\n\n'
            '## 门店网络分布与连锁化进程[S19]全国在营门店数量呈现高速叠加态势。\n\n'
            '## 结论与建议\n\n建议[S1]。')
    out = repair_structure(body)
    assert '## 门店网络分布与连锁化进程\n\n全国在营门店数量呈现高速叠加态势。[S19]' in out


def test_glued_heading_is_a_confirmed_blocking_structure_issue():
    body = ('# 报告\n\n## 执行摘要\n\n摘要[S1]。\n\n'
            '## 二、产能扩张与产业链重构中泰证券统计显示，成本下降80%[S14]。\n\n'
            '## 结论与建议\n\n结论[S1]。')
    issues = [item for item in structure_gaps(body) if item['kind'] == 'glued_heading']
    assert len(issues) == 1
    assert issues[0]['paragraph'] == 4
    assert issues[0]['heading'].startswith('## 二、')
    from services.review_rules import assess_issue, is_confirmed_blocker
    assessed = assess_issue(dict(issues[0], rule='required_structure', kind='required_structure',
                                 structure_kind='glued_heading', severity='hard',
                                 has_section_headings=True), [])
    assert is_confirmed_blocker(assessed)


def test_ambiguous_glued_heading_is_reported_not_guessed():
    body = ('# 报告\n\n## 执行摘要\n\n摘要[S1]。\n\n'
            '## 九、某公司报告显示，增长10%[S1]。\n\n'
            '## 结论与建议\n\n结论[S1]。')
    out = repair_structure(body)
    assert '## 九、某公司报告显示，增长10%[S1]。' in out
    assert any(item['kind'] == 'glued_heading' for item in structure_gaps(out))


def test_clean_numbered_heading_with_data_words_is_not_split():
    body = ('# 报告\n\n## 一、2026年市场数据显示分析\n\n内容[S1]。\n\n'
            '## 执行摘要\n\n摘要[S1]。\n\n## 结论与建议\n\n结论[S1]。')
    out = repair_structure(body)
    assert '## 一、2026年市场数据显示分析' in out
    assert not any(item['kind'] == 'glued_heading' for item in structure_gaps(out))


@pytest.mark.parametrize('heading,expected', [
    ('## 一、执行摘要2026年市场规模达10亿元[S1]。', '## 一、执行摘要\n\n2026年市场规模达10亿元[S1]。'),
    ('## 二、市场规模与增长态势', '## 二、市场规模与增长态势'),
    ('## 一、结论与建议：建议关注风险。', '## 一、结论与建议：建议关注风险。'),
    ('## 二、市场规模，与竞争数据显示，增长10%[S1]。', '## 二、市场规模，与竞争数据显示，增长10%[S1]。'),
    ('## 二、行业市场当前的竞争格局与趋势分析。', '## 二、行业市场当前的竞争格局与趋势分析。'),
    ('## 市场分析发展迅速，增长10%[S1]。', '## 市场分析发展迅速，增长10%[S1]。'),
])
def test_numbered_heading_split_boundaries(heading, expected):
    from services.report_quality import normalize_headings
    assert normalize_headings(heading) == expected


def test_legitimate_spaced_heading_is_not_split():
    body = ('# 报告\n\n## 一、 市场规模\n\n规模内容[S1]。\n\n## 执行摘要\n\n摘要内容[S1]。\n\n'
            '## 结论与建议\n\n结论内容[S1]。')
    assert '## 一、 市场规模' in repair_structure(body)


def test_repair_structure_strips_invalid_fact_like_references():
    body = ("# 报告\n\n## 执行摘要\n\n摘要内容[S1]。\n\n## 结论与建议\n\n"
            "平台格局多元共存[C9925ad1769ee7f328348d1b][S1]。")
    out = repair_structure(body)
    assert "[C9925ad1769ee7f328348d1b]" not in out
    assert "平台格局多元共存[S1]。" in out


def test_orphan_numbered_heading_is_removed_while_content_stays():
    body = ("# 报告\n\n## 一、 规模\n### 1.2\n内容[S1]。\n\n## 执行摘要\n摘要[S1]。\n\n"
            "## 结论与建议\n结论[S1]。")
    out = repair_structure(body)
    assert "### 1.2\n" not in out and "内容[S1]。" in out


def test_numbered_heading_with_title_is_kept():
    body = ("# 报告\n\n## 一、 规模\n### 1.2 结构分化\n内容[S1]。\n\n## 执行摘要\n摘要[S1]。\n\n"
            "## 结论与建议\n结论[S1]。")
    assert "### 1.2 结构分化" in repair_structure(body)


def test_avoidable_gap_is_non_blocking_advice():
    from services.review_rules import assess_issue
    issue = assess_issue(
        dict(kind="avoidable_gap", rule="avoidable_gap", severity="soft",
             sentence="数据待补充：留存率与关税。", candidate_sources=["S2"]),
        [source(source_id="S2", url="https://example.invalid/b", excerpt="2025年出货4200万台")],
    )
    assert issue["decision"] == "suspected" and issue["material"] is False
