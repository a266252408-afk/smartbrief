"""写作质量审计：千分位、错配来源、比较级、待补充、结构、重复与审核可见证据。"""
import asyncio
from unittest.mock import AsyncMock, patch

from agents import reviewer
from services.citations import check_citations
from services.evidence_ranking import review_evidence_block
from services.numbers import parse_number_values
from services.report_quality import duplicate_metrics, ensure_title, structure_gaps
from tests.test_evidence import source


def kinds(result, kind):
    return [item for item in result['binding_issues'] if item['kind'] == kind]


def test_thousands_separator_is_one_number_not_two():
    values = {value for value, _, _ in parse_number_values('1,271亿元')}
    assert values == {1271e8}
    result = check_citations('t', '零售额达1,271亿元，同比下降11.5%[S1]。',
                             [source(excerpt='2025年零售额1,271亿元，同比下降11.5%')])
    assert not kinds(result, 'unbound_number')


def test_price_band_tokens_are_extracted_and_kind_is_checked():
    result = check_citations(
        't',
        '中端（4,000-8,000元）降幅最大，线上6K+、线下10K+保持增长[S1]。',
        [source(excerpt='4,000-8,000元中端价格段降幅显著，3,000元以下实现增长')],
    )
    issues = kinds(result, 'unbound_number')
    assert issues and issues[0]['numbers'] == ['6K', '10K']


def test_unbound_number_reports_candidate_source_for_rebinding():
    result = check_citations(
        't',
        '线上6K+、线下10K+保持增长[S1]。',
        [source(excerpt='中端价格段承压'),
         source(source_id='S2', url='https://example.invalid/b', excerpt='线上6K+、线下10K+市场保持正向增长')],
    )
    issue = kinds(result, 'unbound_number')[0]
    assert issue['candidate_sources'] == ['S2']
    assert issue['severity'] == 'hard'


def test_group_citation_does_not_blame_sources_that_only_carry_other_numbers():
    result = check_citations(
        't',
        '规模54亿美元，2035年增至127亿美元[S1][S2]。',
        [source(excerpt='规模44.6亿美元'),
         source(source_id='S2', url='https://example.invalid/b', excerpt='2026年54亿美元，2035年127亿美元')],
    )
    assert not kinds(result, 'unbound_number')
    assert kinds(result, 'unbound_source')


def test_comparison_requires_source_ordering():
    unsupported = check_citations('t', '占比24.6%，仅次于厨电[S1]。',
                                  [source(excerpt='智能冰箱/洗衣机占比24.6%，智能厨卫电器23.0%')])
    issue = kinds(unsupported, 'unverified_comparison')[0]
    assert issue['severity'] == 'hard'

    supported = check_citations('t', '占比24.6%，仅次于厨电[S1]。',
                                [source(excerpt='智能冰箱/洗衣机占比24.6%，仅次于智能厨卫电器的25.1%')])
    assert not kinds(supported, 'unverified_comparison')


def test_avoidable_gap_lists_source_with_same_metric():
    result = check_citations(
        't',
        '出货量暂作[数据待补充]处理[S1]。',
        [source(excerpt='仅有定性描述'),
         source(source_id='S2', url='https://example.invalid/b',
                excerpt='2025年全球出货规模突破4200万台，较2024年增长18.7%')],
    )
    issue = kinds(result, 'avoidable_gap')[0]
    assert 'S2' in issue['candidate_sources']


def test_unsupported_wording_catches_mechanism_and_threshold_claims():
    wording = check_citations(
        't',
        '万元以上高端市场逆势增长，底层芯片低功耗与边缘计算部署是关键[S1]。',
        [source(excerpt='高端市场保持稳定')],
    )
    issue = kinds(wording, 'unsupported_wording')[0]
    assert {'万元以上', '芯片', '低功耗', '边缘计算'} <= set(issue['terms'])

    supported = check_citations(
        't', '3,000元以下产品实现增长[S1]。',
        [source(excerpt='3,000元以下价格段产品实现增长')],
    )
    assert not kinds(supported, 'unsupported_wording')


def test_structure_gaps_and_complete_report():
    draft = '# 第一章：宏观市场概况\n\n## 1.1 规模\n\n内容[S1]。'
    kinds_found = {item['kind'] for item in structure_gaps(draft)}
    assert kinds_found == {'missing_title', 'missing_summary', 'missing_conclusion'}

    complete = ('# 2026智能冰箱市场调研报告\n\n## 执行摘要\n\n摘要。\n\n'
                '## 第一章 宏观市场概况\n\n内容[S1]。\n\n## 结论与建议\n\n结论。')
    assert structure_gaps(complete) == []

    numbered = ('# 2026智能冰箱市场调研报告\n\n## 执行摘要\n\n摘要。\n\n'
                '## 五、结论与建议\n\n结论。')
    assert structure_gaps(numbered) == []

    numbered_summary = ('# 2026智能冰箱市场调研报告\n\n## 一、执行摘要\n\n摘要。\n\n'
                        '## 五、结论与建议\n\n结论。')
    assert structure_gaps(numbered_summary) == []


def test_ensure_title_prepends_topic_and_is_idempotent():
    draft = '## 第一章 宏观市场概况\n\n内容[S1]。'
    titled = ensure_title(draft, '2026智能冰箱的市场调研')
    assert titled.startswith('# 2026智能冰箱的市场调研报告\n\n')
    assert ensure_title(titled, '主题') == titled
    assert 'missing_title' not in {item['kind'] for item in structure_gaps(titled)}


def test_duplicate_metrics_requires_same_context_and_skips_summary():
    repeated = (
        '# 报告\n\n## 第一章 市场\n\n预计至2027年全球市场总规模触及7800万台[S1]。\n\n'
        '## 第二章 竞争\n\n预计至2027年全球市场总规模触及7800万台[S1]。'
    )
    duplicates = duplicate_metrics(repeated)
    assert duplicates and duplicates[0]['value'] == '7800万'

    different = (
        '# 报告\n\n## 第一章 用户\n\n超过60%的用户愿意支付溢价[S1]。\n\n'
        '## 第二章 渠道\n\n线上渠道占比超60%[S1]。'
    )
    assert not duplicate_metrics(different)

    summary_only = (
        '# 报告\n\n## 执行摘要\n\n市场规模7800万台。\n\n'
        '## 第一章 市场\n\n全球市场总规模触及7800万台[S1]。'
    )
    assert not duplicate_metrics(summary_only)


def test_review_evidence_block_pins_cited_source_even_if_low_value():
    block = review_evidence_block(
        '草稿引用S2', [source(excerpt='只有定性描述'), source(source_id='S2', url='https://example.invalid/b',
                                                       excerpt='关键数字4200万台')],
        required_ids=['S2'],
    )
    assert '[S2]' in block and '关键数字4200万台' in block


class _Resp:
    def __init__(self, content):
        self.choices = [type('C', (), {'message': type('M', (), {'content': content})()})()]


def test_reviewer_comment_names_candidate_source_for_rebinding():
    with patch.object(reviewer, 'chat', new=AsyncMock(return_value=_Resp('{"passed": true, "comments": []}'))):
        out = asyncio.run(reviewer.reviewer_node({
            'task_id': 't',
            'draft_content': '线上6K+保持增长[S1]。',
            'evidence': [source(excerpt='只有定性描述'),
                         source(source_id='S2', url='https://example.invalid/b',
                                excerpt='线上6K+市场保持正向增长')],
        }))
    assert out['passed'] is False
    assert 'S2' in out['review_comments'][0]


def test_reviewer_hard_rejects_unsupported_comparison():
    with patch.object(reviewer, 'chat', new=AsyncMock(return_value=_Resp('{"passed": true, "comments": []}'))):
        out = asyncio.run(reviewer.reviewer_node({
            'task_id': 't',
            'draft_content': '占比24.6%，仅次于厨电[S1]。',
            'evidence': [source(excerpt='智能冰箱/洗衣机占比24.6%，智能厨卫电器23.0%')],
        }))
    assert out['passed'] is False
    assert out['quality_status'] == 'rejected'
    assert out['error_code'] == 'CLAIM_UNSUPPORTED'
    assert any('比较级' in comment for comment in out['review_comments'])
