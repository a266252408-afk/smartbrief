"""以新任务保留重生成溯源，不覆盖原报告或修改其质量。"""
from pydantic import BaseModel, ConfigDict
from typing import Literal


class TaskNotTerminal(ValueError):
    pass


class TaskRegenerated(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    topic: str
    status: Literal["queued"]
    regenerated_from: str


def prepare_regeneration(task_id: str, key=None) -> dict | None:
    from db.task_control_repository import create_job
    result = create_job(source_id=task_id,key=key)
    return TaskRegenerated.model_validate(result).model_dump() if result is not None else None
