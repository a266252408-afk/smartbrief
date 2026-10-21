"""v3质量、引用、用量与人工抽查指标；纯确定性计算。"""
from collections import Counter
from evaluation.metrics import compute_p95
from pydantic import BaseModel, ConfigDict, Field


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


def aggregate(results, total_cases):
    if type(total_cases) is not int or total_cases < 1 or len(results) > total_cases:
        raise ValueError('评测分母须为正整数且不能小于已完成数量')
    passed = [r for r in results if r['execution_success'] is True and r['quality_passed'] is True and not r['forced_output']]
    completed = [r for r in results if r['execution_success'] is True]
    durations = [r['duration_seconds'] for r in completed if r['duration_seconds'] is not None]
    citations = sum(r['citation_count'] for r in results)
    valid = sum(r['citation_valid_count'] for r in results)
    costs = {}
    for row in results:
        for currency, amount in row['estimated_costs'].items():
            costs[currency] = costs.get(currency, 0.0) + amount
    known = bool(results) and all(r['input_tokens'] is not None and r['output_tokens'] is not None for r in results)
    return dict(metric_version=3, total_cases=total_cases, completed_cases=len(results), pending_cases=total_cases-len(results),
        quality_pass_rate=len(passed)/total_cases, execution_completion_rate=len(completed)/total_cases,
        citation_count=citations, citation_valid_count=valid, citation_valid_rate=valid/citations if citations else None,
        input_tokens=sum(r['input_tokens'] for r in results) if known else None,
        output_tokens=sum(r['output_tokens'] for r in results) if known else None,
        estimated_costs=costs, cost_complete=len(results)==total_cases and all(r['cost_complete'] for r in results),
        p95_completed_seconds=compute_p95(durations) if durations else None,
        failure_categories=dict(Counter(r['failure_category'] or ('FORCED_OUTPUT' if r['forced_output'] else 'QUALITY_NOT_PASSED') for r in results if r not in passed)),
        manual_fact_check=dict(status='pending', sample_size=0, accuracy=None))
