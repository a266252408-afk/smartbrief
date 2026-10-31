"""有限普通重试/公开诊断反馈实验契约。"""
from typing import Literal
from pathlib import Path
from time import perf_counter
from pydantic import BaseModel, ConfigDict, Field, model_validator
from evaluation import catalog
from evaluation.full_task import FullTaskResult, DEVELOPMENT_CASES, run_case as full_case


class ExperimentRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    profile: Literal['bounded_experiment'] = 'bounded_experiment'
    case_ids: list[str] = Field(min_length=1, max_length=2)
    max_attempts: int = Field(default=2, ge=1, le=2)
    call_limit: int = Field(default=10, ge=1, le=10)

    @model_validator(mode='after')
    def cases(self):
        if len(set(self.case_ids)) != len(self.case_ids) or not set(self.case_ids) <= set(DEVELOPMENT_CASES):
            raise ValueError('仅允许开发组公开输入')
        return self


class ExperimentArm(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    mode: Literal['plain_retry', 'reflection']
    attempts: list[FullTaskResult]
    provider_calls: int | None
    ever_passed: bool
    always_passed: bool


class ExperimentCaseResult(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    case_id: str
    check_status: Literal['pass', 'fail', 'unknown']
    report_quality: Literal['unknown'] = 'unknown'
    baseline: FullTaskResult
    arms: list[ExperimentArm]
    duration_seconds: float
    error: str | None


def validate_sources(case_ids):
    if not set(case_ids) <= set(DEVELOPMENT_CASES) or any(catalog.detail(cid)['case']['split'] != 'development' for cid in case_ids):
        raise ValueError('留出与仅验收案例不能用于反思迭代')


async def run_case(case_id, directory, *, max_attempts=2, call_limit=10, executor=None, on_update=None):
    body = ExperimentRequest(case_ids=[case_id], max_attempts=max_attempts, call_limit=call_limit)
    validate_sources(body.case_ids)
    executor = executor or full_case
    directory = Path(directory)
    start = perf_counter()
    async def execute(phase, attempt, feedback='', fault='none'):
        result = FullTaskResult.model_validate(await executor(case_id, 'primary', directory / phase / str(attempt),
            max_calls=call_limit, feedback=feedback, fault=fault)).model_dump()
        if result['case_id'] != case_id or result['provider_calls'] is not None and result['provider_calls'] > call_limit:
            raise ValueError('结果归属或供应商尝试预算不一致')
        if on_update:
            await on_update(dict(case_id=case_id, phase=phase, attempt=attempt, result=result))
        return result
    baseline = await execute('baseline', 0, fault='both_unavailable')
    arms = []
    error = None
    if baseline['check_status'] == 'unknown' or baseline['provider_calls'] is None or baseline['check_status'] == 'pass':
        error = '初始故障未形成有效失败凭据，不能宣称完成实验'
    else:
        for mode in ('plain_retry', 'reflection'):
            attempts, previous = [], baseline
            for number in range(1, max_attempts + 1):
                # 只读取公开执行状态；不读取oracle、评分标准或黄金事实。
                feedback = (f"上一轮工作流状态：{previous['task_execution_status']}；公开处理状态：{previous['check_status']}。"
                    '重新检查模型可用性，只根据本次给定资料作答，缺口保持可见。') if mode == 'reflection' else ''
                previous = await execute(mode, number, feedback)
                attempts.append(previous)
                if previous['check_status'] == 'pass' or previous['provider_calls'] is None:
                    break
            count = sum(r['provider_calls'] for r in attempts) if all(r['provider_calls'] is not None for r in attempts) else None
            arms.append(ExperimentArm(mode=mode, attempts=attempts, provider_calls=count,
                ever_passed=any(r['check_status'] == 'pass' for r in [baseline] + attempts),
                always_passed=all(r['check_status'] == 'pass' for r in [baseline] + attempts)))
    status = 'unknown' if error or any(a.provider_calls is None for a in arms) else 'pass' if all(a.ever_passed for a in arms) else 'fail'
    return ExperimentCaseResult(case_id=case_id, check_status=status, baseline=baseline, arms=arms,
        duration_seconds=round(perf_counter()-start, 3), error=error).model_dump()
