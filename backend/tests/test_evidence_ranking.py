"""证据排序、数字绑定与审核可见证据的行为测试。"""
import asyncio
import json
from datetime import date
from unittest.mock import AsyncMock, patch

from agents import reviewer
from services.citations import check_citations, evidence_context
from services.evidence_ranking import (
    classify_role,
    metric_key,
    parse_published_at,
    rank_sources,
    review_evidence_block,
)
from tests.test_evidence import source

TODAY = date(2026, 11, 3)
RETRIEVED = '2026-11-03T00:00:00+00:00'


def pc_sources():
    return [
        source(source_id='S1', title='IDC 2026 global PC forecast',
               excerpt='IDC预计2026年全球PC出货量下降11.3%。',
               url='https://www.idc.com/a', retrieved_at=RETRIEVED),
        source(source_id='S2', title='Gartner Q3 2026 PC shipments',
               excerpt='Gartner初步统计显示，2026年第三季度全球PC出货量为5540万台，同比下降22.4%。',
               url='https://www.gartner.com/newsroom/2026-10-20/q3', retrieved_at=RETRIEVED),
        source(source_id='S3', title='付费报告摘要',
               excerpt='Market Forecast 2026-2030，17 slides。',
               url='https://my.idc.com/x', retrieved_at=RETRIEVED),
        source(source_id='S4', title='IDC 2026 中国PC市场',
               excerpt='IDC预计2026年中国PC市场总出货量约为4038万台，同比微降6.1%。',
               url='https://www.ithome.com/a', retrieved_at=RETRIEVED),
        source(source_id='S5', title='IDC 2026 中国PC市场十大洞察',
               excerpt='IDC预计2026年中国PC市场出货量达4222万台，同比微降0.8%。',
               url='https://www.xcc.com/b', retrieved_at=RETRIEVED),
    ]


def test_rank_keeps_latest_actual_and_china_conflict_pair():
    ranked = rank_sources('2026年全球及中国PC出货量', pc_sources(), budget=12000, today=TODAY)
    selected = [item['source_id'] for item in ranked['selected']]
    dropped = {item['source_id']: item['reason'] for item in ranked['dropped']}
    assert 'S2' in selected                       # 最新 Q3 实绩
    assert {'S4', 'S5'} <= set(selected)          # 同一指标的两个冲突预测都保留
    assert dropped['S3'] == 'low_value'           # 无可用数字的付费摘要被排除


def test_evidence_context_exposes_selection_and_actual_first():
    context = json.loads(evidence_context({}, pc_sources(), budget=12000, query='2026年全球及中国PC出货量'))
    assert context['sources'][0]['source_id'] == 'S2'
    assert context['sources'][0]['role'] == 'actual'
    assert context['sources'][0]['published_at'] == '2026-10-20'
    selection = context['selection']
    assert 'S2' in selection['selected'] and 'S4' in selection['selected']
    assert any(item['source_id'] == 'S3' for item in selection['dropped'])
    assert len(json.dumps(context, ensure_ascii=False)) <= 12000


def test_evidence_context_small_budget_does_not_force_sources():
    context = json.loads(evidence_context({}, pc_sources(), budget=256, query='PC出货量'))
    assert context['sources'] == []
    assert 'selection' not in context


def test_binding_accepts_unit_conversion_between_亿_and_million():
    check = check_citations(
        't',
        '2026年第二季度总出货约1.03亿台，同比下降6.8%[S1]。',
        [source(excerpt='Worldwide shipments totaled about 103 million units, down 6.8% year over year.')],
    )
    assert not [item for item in check['binding_issues'] if item['kind'] == 'unbound_number']


def test_binding_flags_number_missing_from_cited_source():
    check = check_citations('t', '出货量5540万台[S1]。', [source(excerpt='其他资料')])
    assert check['valid'] is True                 # 结构仍有效，语义绑定单独报告
    issues = [item for item in check['binding_issues'] if item['kind'] == 'unbound_number']
    assert issues and issues[0]['numbers'] == ['5540万']


def test_binding_flags_over_citation_but_keeps_number_covered():
    check = check_citations(
        't', '增长10%[S1][S2]。',
        [source(excerpt='收入增长10%'),
         source(source_id='S2', url='https://example.invalid/b', excerpt='其他资料')],
    )
    assert not [item for item in check['binding_issues'] if item['kind'] == 'unbound_number']
    assert any(item['kind'] == 'unbound_source' and item['source_id'] == 'S2'
               for item in check['binding_issues'])


class _Resp:
    def __init__(self, content):
        self.choices = [type('C', (), {'message': type('M', (), {'content': content})()})()]


def test_reviewer_hard_rejects_unbound_number_even_if_model_passes():
    with patch.object(reviewer, 'chat', new=AsyncMock(return_value=_Resp('{"passed": true, "comments": []}'))):
        out = asyncio.run(reviewer.reviewer_node({
            'task_id': 't',
            'draft_content': '出货量5540万台[S1]。',
            'evidence': [source(excerpt='其他资料')],
        }))
    assert out['passed'] is False
    assert out['quality_status'] == 'rejected'
    assert out['error_code'] == 'CITATION_UNBOUND'
    assert any('5540' in comment for comment in out['review_comments'])
    assert any('5540' in error for error in out['citation_errors'])


def test_reviewer_prompt_contains_evidence_excerpt():
    captured = {}

    async def fake_chat(messages, **kwargs):
        captured['user'] = messages[1]['content']
        return _Resp('{"passed": true, "comments": []}')

    with patch.object(reviewer, 'chat', new=fake_chat):
        asyncio.run(reviewer.reviewer_node({
            'task_id': 't',
            'draft_content': '## 草稿[S1]',
            'evidence': [source(excerpt='关键证据片段')],
        }))
    assert '关键证据片段' in captured['user']
    assert '确定性引用绑定检查' in captured['user']


def test_parse_published_at_prefers_full_date_and_latest_year():
    assert parse_published_at(source(url='https://www.gartner.com/newsroom/2026-10-20/x'), TODAY.year) == date(2026, 10, 20)
    assert parse_published_at(source(excerpt='Published: 29 April 2026'), TODAY.year) == date(2026, 4, 29)
    assert parse_published_at(source(excerpt='北京，2026年3月12日'), TODAY.year) == date(2026, 3, 12)
    assert parse_published_at(source(title='2024-2026年中国PC市场'), TODAY.year) == date(2026, 1, 1)
    assert parse_published_at(source(title='Forecasts 2031', excerpt='market size 2026'), TODAY.year) == date(2026, 1, 1)


def test_role_classification_and_metric_key():
    actual = source(title='Gartner Q3 2026 PC shipments',
                    excerpt='Gartner初步统计显示，2026年第三季度全球PC出货量为5540万台。',
                    url='https://www.gartner.com/a')
    forecast = source(title='IDC 2026 global PC forecast',
                      excerpt='IDC预计2026年全球PC出货量下降11.3%。',
                      url='https://www.idc.com/b')
    china = source(title='IDC 2026 中国PC市场',
                   excerpt='2026年中国PC市场出货量达4222万台。',
                   url='https://www.xcc.com/c')
    ai_pc = source(title='AI PC 竞品分析：渗透率过半',
                   excerpt='2026年全球 AI PC 渗透率达到 54.7%。',
                   url='https://deepreport.cn/d')
    assert classify_role(actual) == 'actual'
    assert classify_role(forecast) == 'forecast'
    assert metric_key(actual) == 'global:2026Q3:pc:shipments'
    assert metric_key(china) == 'china:2026:pc:shipments'
    assert metric_key(ai_pc) == 'global:2026:ai_pc:penetration'


def test_document_dedupe_uses_chunk_not_title():
    first = source(kind='document', url=None, document_id='d' * 32, chunk_id='chunk-1',
                   title='同一文档', excerpt='2026年出货量137台。')
    second = source(source_id='S2', kind='document', url=None, document_id='d' * 32, chunk_id='chunk-2',
                    title='同一文档', excerpt='2026年出货量138台。')
    ranked = rank_sources('文档内容', [first, second], budget=12000, today=TODAY)
    assert [item['source_id'] for item in ranked['selected']] == ['S1', 'S2']


def test_low_value_only_source_still_provides_context():
    ranked = rank_sources('PC市场', [source(excerpt='只有定性描述，没有数字')], budget=12000, today=TODAY)
    assert [item['source_id'] for item in ranked['selected']] == ['S1']


def test_binding_ignores_version_numbers():
    check = check_citations('t', '行业迈入2.0时代，市场规模增长10%[S1]。', [source(excerpt='增长10%')])
    assert not [item for item in check['binding_issues'] if item['kind'] == 'unbound_number']
    only_version = check_citations('t', '行业迈入2.0时代[S1]。', [source(excerpt='其他资料')])
    assert not only_version['binding_issues']


def test_required_source_is_forced_even_without_numbers():
    sources = [
        source(source_id='S1', title='有数字来源', excerpt='2026年增长10%', url='https://example.invalid/a'),
        source(source_id='S2', title='待保留来源', excerpt='只有定性描述', url='https://example.invalid/b'),
    ]
    context = json.loads(evidence_context({}, sources, budget=12000, query='市场增长', required_ids=['S2']))
    assert 'S2' in [item['source_id'] for item in context['sources']]


def test_more_than_sixteen_required_sources_stay_in_context():
    """必保来源不能被 16 条候选上限截断，否则返工稿必然丢失引用。"""
    sources = [
        source(source_id=f'S{i}', title=f'来源{i}', excerpt=f'2026年增长{i}%',
               url=f'https://example.invalid/{i}')
        for i in range(1, 19)
    ]
    required = [f'S{i}' for i in range(1, 19)]
    context = json.loads(evidence_context({}, sources, budget=20000, query='市场增长', required_ids=required))
    assert {item['source_id'] for item in context['sources']} == set(required)


def test_review_block_lists_writer_selection_and_drops():
    block = review_evidence_block(
        '草稿', [source()],
        selection={'selected': ['S1'], 'dropped': [{'source_id': 'S2', 'reason': 'budget'}]},
    )
    assert 'writer 实际使用：S1' in block
    assert 'writer 未使用：S2（budget）' in block
