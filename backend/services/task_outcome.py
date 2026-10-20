"""任务结果公共类型；执行与质量分别表达。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, StrictBool

ExecutionStatus = Literal["queued", "running", "paused", "completed", "failed", "cancelled"]
QualityStatus = Literal["pending", "passed", "rejected", "unavailable", "not_run", "unknown"]


class TaskOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    execution_status: ExecutionStatus
    quality_status: QualityStatus
    forced_output: StrictBool
    error_code: str | None = None
