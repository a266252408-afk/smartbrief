"""真实质量指标不能由执行完成、强制输出或部分结果冒充。"""
import pytest
from evaluation.quality import aggregate


def row(**changes):
    return dict(execution_success=True, quality_passed=True, forced_output=False, duration_seconds=2.0, citation_valid_count=1, citation_count=1, input_tokens=10, output_tokens=3, estimated_costs={}, cost_complete=False, failure_category=None) | changes


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
