"""v3质量、引用、用量与人工抽查指标；纯确定性计算。

质量口径分为三部分：
- 整体质量通过率：全部用例为分母，保留兼容展示，不作为可达成目标；
- 正常用例质量通过率：只统计非故障用例，故障用例不应拉低内容质量；
- 故障处理正确率：故障用例必须被识别、不能冒充质量通过，且失败分类符合场景。
分组合格率仅在全部用例处理完后给出；未跑完返回 None，避免用部分样本宣称达标。
"""
from collections import Counter
from evaluation.metrics import compute_p95
from pydantic import BaseModel, ConfigDict, Field

# 故障场景 -> 可接受的失败分类。故障用例“处理正确”指：不冒充质量通过、
# 不靠强制输出收尾，并把失败原因落到对应分类；执行是否完成取决于场景本身。
FAULT_EXPECTED_CATEGORIES = {
    'model_timeout': {'MODEL_TIMEOUT', 'TASK_TIMEOUT', 'RESEARCH_TIMEOUT', 'DRAFT_UNAVAILABLE'},
    'review_invalid': {'REVIEW_UNAVAILABLE', 'REVIEW_INVALID', 'QUALITY_UNAVAILABLE'},
    'budget': {'BUDGET_EXCEEDED'},
    'cancel': {'TASK_CANCELLED', 'CANCELLED'},
}
KNOWN_CATEGORIES = {'regular', 'uncovered', 'evidence', 'fault'}


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    case_id: str
    case_index: int
    category: str
    scenario: str
    topic: str
    keywords: list[str]
    task_id: str
    success: bool
    execution_success: bool
    quality_passed: bool
    quality_status: str
    forced_output: bool
    forced_pass: bool
    failure_category: str | None
    duration_seconds: float = Field(ge=0)
    react_loops: int = Field(ge=0)
    keyword_coverage: float = Field(ge=0,le=1)
    memory_hits: int = Field(ge=0)
    citation_count: int = Field(ge=0)
    citation_valid_count: int = Field(ge=0)
    citation_valid_rate: float | None = Field(ge=0,le=1)
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    estimated_costs: dict[str,float]
    cost_complete: bool
    report_sha256: str | None
    error: str | None
    manual_fact_check: dict


def _passed(row):
    """质量通过：执行完成、质量通过，且不是强制输出。"""
    return row.get('execution_success') is True and row.get('quality_passed') is True and not row.get('forced_output')


def _false_pass(row):
    """故障用例被当成质量通过（含强制输出以外的任何放行）。"""
    return row.get('quality_passed') is True and not row.get('forced_output')


def _fault_handled(row):
    """故障用例是否被正确识别并记录，而不是静默失败或冒充通过。"""
    if _false_pass(row) or row.get('forced_output'):
        return False
    category = row.get('failure_category')
    if not category:
        return False
    expected = FAULT_EXPECTED_CATEGORIES.get(row.get('scenario') or '')
    return expected is None or category in expected


def aggregate(results, total_cases):
    if type(total_cases) is not int or total_cases < 1 or len(results) > total_cases:
        raise ValueError('评测分母须为正整数且不能小于已完成数量')
    passed = [r for r in results if _passed(r)]
    completed = [r for r in results if r.get('execution_success') is True]
    durations = [r['duration_seconds'] for r in completed if r['duration_seconds'] is not None]
    citations = sum(r['citation_count'] for r in results)
    valid = sum(r['citation_valid_count'] for r in results)
    costs = {}
    for row in results:
        for currency, amount in row['estimated_costs'].items():
            costs[currency] = costs.get(currency, 0.0) + amount
    known = bool(results) and all(r['input_tokens'] is not None and r['output_tokens'] is not None for r in results)

    # 分组指标：只有所有结果都带 category 且全部用例已处理完才给出比率。
    group_known = bool(results) and all(r.get('category') in KNOWN_CATEGORIES for r in results)
    normal = [r for r in results if r.get('category') in {'regular', 'uncovered', 'evidence'}]
    faults = [r for r in results if r.get('category') == 'fault']
    normal_passed = [r for r in normal if _passed(r)]
    fault_handled = [r for r in faults if _fault_handled(r)]
    fault_false_pass = [r for r in faults if _false_pass(r)]
    complete = len(results) == total_cases

    normal_total_cases = len(normal) if group_known else None
    normal_passed_cases = len(normal_passed) if group_known else None
    fault_total_cases = len(faults) if group_known else None
    fault_handled_cases = len(fault_handled) if group_known else None
    fault_false_pass_cases = len(fault_false_pass) if group_known else None
    normal_quality_pass_rate = (
        round(len(normal_passed) / len(normal), 4)
        if complete and group_known and normal else None
    )
    fault_handling_rate = (
        round(len(fault_handled) / len(faults), 4)
        if complete and group_known and faults else None
    )

    return dict(
        metric_version=3,
        total_cases=total_cases,
        completed_cases=len(results),
        pending_cases=total_cases-len(results),
        quality_pass_rate=len(passed)/total_cases,
        execution_completion_rate=len(completed)/total_cases,
        normal_total_cases=normal_total_cases,
        normal_passed_cases=normal_passed_cases,
        normal_quality_pass_rate=normal_quality_pass_rate,
        fault_total_cases=fault_total_cases,
        fault_handled_cases=fault_handled_cases,
        fault_false_pass_cases=fault_false_pass_cases,
        fault_handling_rate=fault_handling_rate,
        citation_count=citations,
        citation_valid_count=valid,
        citation_valid_rate=valid/citations if citations else None,
        input_tokens=sum(r['input_tokens'] for r in results) if known else None,
        output_tokens=sum(r['output_tokens'] for r in results) if known else None,
        estimated_costs=costs,
        cost_complete=len(results)==total_cases and all(r['cost_complete'] for r in results),
        p95_completed_seconds=compute_p95(durations) if durations else None,
        failure_categories=dict(Counter(r['failure_category'] or ('FORCED_OUTPUT' if r['forced_output'] else 'QUALITY_NOT_PASSED') for r in results if not _passed(r))),
        manual_fact_check=dict(status='pending', sample_size=0, accuracy=None),
    )
