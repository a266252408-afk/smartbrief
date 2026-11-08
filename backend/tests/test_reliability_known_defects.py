"""R00固定缺陷和配对反例；调用真实纯函数，不替换业务判定。"""
import pytest
from services.fact_binding import BindingError, render_bound_text
from services.report_quality import structure_gaps
from services.candidate_acceptance import validate_candidate


def test_d01_organizational_transition_needs_no_fact_reference():
    text = '下文将从需求、竞争和风险三个方面展开分析。'
    assert render_bound_text(text, 'task_baseline', {})[0] == text


def test_d01_numeric_statement_disguised_as_transition_is_rejected():
    with pytest.raises(BindingError):
        render_bound_text('下文将说明市场预计增长20%。', 'task_baseline', {})


def test_d02_equivalent_section_titles_are_recognized():
    body = '# 行业报告\n\n## 核心摘要\n\n资料不足，结论尚待核实。\n\n## 主要结论\n\n建议继续收集资料。'
    assert not structure_gaps(body)


def test_d02_absent_sections_are_still_detected():
    assert {i['kind'] for i in structure_gaps('# 行业报告\n\n## 数据\n\n已有材料。')} >= {'missing_summary', 'missing_conclusion'}


def _check(sentence):
    return {'errors': [], 'binding_issues': [dict(kind='unbound_number', severity='hard', sentence=sentence, numbers=['99亿元'], source_ids=['S1'])]}


@pytest.mark.xfail(strict=True, reason="R00已复现；R02负责修复")
def test_d03_reworded_existing_conflict_is_not_introduced():
    validate_candidate('市场规模约99亿元。', '该市场规模约99亿元。',
                       _check('市场规模约99亿元。'), _check('该市场规模约99亿元。'))


def test_d03_new_number_is_rejected():
    new = _check('市场规模约199亿元。')
    new['binding_issues'][0]['numbers'] = ['199亿元']
    with pytest.raises(ValueError):
        validate_candidate('旧稿', '新稿', _check('市场规模约99亿元。'), new)
