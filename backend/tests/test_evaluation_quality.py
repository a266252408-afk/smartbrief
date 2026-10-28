"""真实质量指标不能由执行完成、强制输出或部分结果冒充。"""
import pytest
from evaluation.quality import aggregate


def row(**changes):
    return dict(category='regular', scenario='normal', execution_success=True, quality_passed=True, forced_output=False, duration_seconds=2.0, citation_valid_count=1, citation_count=1, input_tokens=10, output_tokens=3, estimated_costs={}, cost_complete=False, failure_category=None) | changes


def test_partial_failed_forced_unknown_denominators():
    result = aggregate([row(), row(forced_output=True), row(execution_success=False,quality_passed=False,failure_category='MODEL_TIMEOUT',citation_count=0,citation_valid_count=0,input_tokens=None,output_tokens=None)], 20)
    assert result['quality_pass_rate'] == 0.05
    assert result['execution_completion_rate'] == 0.1
    assert result['completed_cases'] == 3 and result['pending_cases'] == 17
    assert result['citation_valid_rate'] == 1 and result['citation_count'] == 2
    assert result['input_tokens'] is None and result['output_tokens'] is None
    assert result['estimated_costs'] == {} and result['cost_complete'] is False
    assert result['manual_fact_check'] == dict(status='pending',sample_size=0,accuracy=None)
    assert result['failure_categories']['MODEL_TIMEOUT'] == 1


def test_empty_unknown_and_p95_currency_separation():
    empty = aggregate([],20)
    assert empty['citation_valid_rate'] is None and empty['p95_completed_seconds'] is None
    assert empty['input_tokens'] is None
    result = aggregate([row(duration_seconds=float(i),estimated_costs={'USD':0.1},cost_complete=True) for i in range(1,21)],20)
    assert result['p95_completed_seconds'] == 19
    assert result['estimated_costs']['USD'] == pytest.approx(2)
    assert result['cost_complete'] and result['input_tokens'] == 200
    mixed = aggregate([row(estimated_costs={'USD':1.0},cost_complete=True),row(estimated_costs={'CNY':2.0},cost_complete=True)],2)
    assert mixed['estimated_costs'] == dict(USD=1.0,CNY=2.0)


def test_group_rates_do_not_let_fault_cases_lower_normal_quality():
    """4 个故障用例按设计不可能质量通过，不能把 90% 目标变成永远不达标。"""
    normal = [
        row(), row(), row(),
        row(category='evidence', scenario='no_sources', execution_success=True,
            quality_passed=False, failure_category='CITATION_INVALID'),
    ]
    faults = [
        row(category='fault', scenario='model_timeout', execution_success=False,
            quality_passed=False, failure_category='MODEL_TIMEOUT', citation_count=0, citation_valid_count=0),
        row(category='fault', scenario='review_invalid', execution_success=True,
            quality_passed=False, failure_category='REVIEW_UNAVAILABLE'),
        row(category='fault', scenario='budget', execution_success=False,
            quality_passed=False, failure_category='BUDGET_EXCEEDED', citation_count=0, citation_valid_count=0),
        row(category='fault', scenario='cancel', execution_success=False,
            quality_passed=False, failure_category='TASK_CANCELLED', citation_count=0, citation_valid_count=0),
    ]
    result = aggregate(normal + faults, 8)
    assert result['normal_total_cases'] == 4 and result['normal_passed_cases'] == 3
    assert result['normal_quality_pass_rate'] == 0.75
    assert result['fault_total_cases'] == 4 and result['fault_handled_cases'] == 4
    assert result['fault_false_pass_cases'] == 0
    assert result['fault_handling_rate'] == 1.0
    # 整体通过率仍是 3/8，但只作为参考口径。
    assert result['quality_pass_rate'] == 0.375


def test_fault_false_pass_and_unknown_category_are_not_handled():
    false_pass = row(category='fault', scenario='budget', execution_success=True,
                     quality_passed=True, failure_category=None)
    handled = aggregate([false_pass], 1)
    assert handled['fault_false_pass_cases'] == 1
    assert handled['fault_handled_cases'] == 0 and handled['fault_handling_rate'] == 0.0

    # 故障发生了但没有失败分类，不能算“正确识别”。
    unknown_reason = row(category='fault', scenario='budget', execution_success=False,
                         quality_passed=False, failure_category=None)
    no_reason = aggregate([unknown_reason], 1)
    assert no_reason['fault_handled_cases'] == 0 and no_reason['fault_handling_rate'] == 0.0

    # 没有 category 的旧结果不能推断分组，指标为 None 而不是伪造 0%。
    unknown = aggregate([row(category=None)], 1)
    assert unknown['normal_total_cases'] is None and unknown['normal_quality_pass_rate'] is None


def test_group_rates_are_unknown_until_all_cases_processed():
    partial = aggregate([row(), row(category='fault', scenario='budget', execution_success=False,
                                   quality_passed=False, failure_category='BUDGET_EXCEEDED')], 20)
    assert partial['normal_total_cases'] == 1 and partial['fault_total_cases'] == 1
    assert partial['normal_quality_pass_rate'] is None
    assert partial['fault_handling_rate'] is None
