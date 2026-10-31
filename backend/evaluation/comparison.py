"""跨版本对照契约，原protocol-1比较规则保持。"""
from typing import Literal
from datetime import datetime, timezone
import hashlib
import json
import math
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    baseline_id: str = Field(pattern=r'^er_[0-9a-f]{32}$')
    current_id: str = Field(pattern=r'^er_[0-9a-f]{32}$')
    changed_dimension: Literal['code', 'model_group']

    @model_validator(mode='after')
    def distinct(self):
        if self.baseline_id == self.current_id:
            raise ValueError('对照需要两次不同运行')
        return self


class Comparison(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    comparison_id: str
    baseline_id: str
    current_id: str
    changed_dimension: Literal['code', 'model_group']
    attribution_allowed: bool
    baseline_sha256: str
    current_sha256: str
    reasons: list[str]
    differences: list[str]
    case_changes: list[dict]
    execution_pass_rate_delta: float | None
    quality_pass_rate_delta: float | None
    all_attempt_seconds_delta: float | None
    created_at: str
    interpretation: str


def compare(body, baseline, current):
    frozen = ('dataset_hash', 'source_hash', 'model', 'prompt_version', 'engine', 'model_group',
              'feature_flags', 'execution_budget', 'memory_protocol', 'randomness')
    declared = 'source_hash' if body.changed_dimension == 'code' else 'model_group'
    a, b = baseline.get('metadata') or {}, current.get('metadata') or {}
    reasons = []
    if baseline.get('profile') != 'full_task_local' or current.get('profile') != 'full_task_local':
        reasons.append('只支持冻结资料本地完整图协议归因；其他类型或真实模型仅并排')
    for record in (baseline, current):
        ids, rows, summary = record.get('case_ids') or [], record.get('results') or [], record.get('summary') or {}
        if (record.get('execution_status') != 'completed' or not ids or len(ids) != len(set(ids))
                or len(rows) != len(ids) or [r.get('case_id') for r in rows] != ids
                or summary.get('planned') != len(ids) or summary.get('processed') != len(ids)
                or any(r.get('check_status') not in ('pass', 'fail', 'unknown') for r in rows)):
            reasons.append('运行未完整处理全部计划案例或结果归属不一致')
    if baseline.get('case_ids') != current.get('case_ids'):
        reasons.append('案例与顺序不同，不能挑选成功子集比较')
    missing = [k for k in frozen if a.get(k) in (None, '') or b.get(k) in (None, '')]
    if missing:
        reasons.append('缺少冻结元数据：' + ', '.join(missing))
    for meta in (a, b):
        flags = meta.get('feature_flags')
        budget = meta.get('execution_budget')
        if (not isinstance(flags, dict) or set(flags) != {'rag', 'memory', 'review'}
                or any(type(value) is not bool for value in flags.values())
                or not isinstance(budget, dict) or not budget or any(value is None for value in budget.values())):
            reasons.append('开关或资源预算未完整冻结')
    differences = [k for k in frozen if a.get(k) != b.get(k)]
    if differences != [declared]:
        reasons.append('实际变化必须且只能是声明维度：' + declared)
    allowed = not reasons
    changes = []
    execution_delta = quality_delta = time_delta = None
    if allowed:
        left, right = baseline['results'], current['results']
        for old, new in zip(left, right):
            x, y = old['check_status'], new['check_status']
            transition = ('regression' if x == 'pass' and y != 'pass' else 'improvement' if x == 'fail' and y == 'pass'
                          else 'unknown' if 'unknown' in (x, y) else 'unchanged')
            changes.append(dict(case_id=old['case_id'], baseline=x, current=y, transition=transition))
        execution_delta = sum(r['check_status'] == 'pass' for r in right) / len(right) - sum(r['check_status'] == 'pass' for r in left) / len(left)
        if (all(a.get(k) is not None and a.get(k) == b.get(k) for k in ('grader_version', 'rubric_hash'))
                and all(r.get('report_quality') in ('passed', 'rejected') for r in left + right)):
            quality_delta = sum(r['report_quality'] == 'passed' for r in right) / len(right) - sum(r['report_quality'] == 'passed' for r in left) / len(left)
        durations = [r.get('duration_seconds') for r in left + right]
        if all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in durations):
            time_delta = sum(r['duration_seconds'] for r in right) - sum(r['duration_seconds'] for r in left)
    def digest(record):
        return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode('utf8')).hexdigest()
    return Comparison(**body.model_dump(), comparison_id='ec_' + uuid4().hex, attribution_allowed=allowed,
        baseline_sha256=digest(baseline), current_sha256=digest(current), reasons=list(dict.fromkeys(reasons)), differences=differences,
        case_changes=changes, execution_pass_rate_delta=execution_delta, quality_pass_rate_delta=quality_delta,
        all_attempt_seconds_delta=time_delta, created_at=datetime.now(timezone.utc).isoformat(),
        interpretation='仅固定资料软件协议差异；不证明真实模型质量增益、因果关系或统计显著性。未知质量不填零。').model_dump()
