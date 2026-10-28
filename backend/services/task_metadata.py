"""任务元数据：生成版本与显式演示标识；演示任务必须可追溯样例版本。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator, model_validator


class TaskMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    demo: StrictBool = False
    generator_version: str | None = None
    demo_sample_id: str | None = None
    demo_sample_version: str | None = None
    execution_mode: Literal['online', 'offline'] | None = None
    evaluation_id: str | None = None
    evaluation_case_id: str | None = None

    @field_validator("generator_version", "demo_sample_id", "demo_sample_version", "evaluation_id", "evaluation_case_id")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("元数据字段不能为空白")
        return value

    @model_validator(mode="after")
    def demo_fields(self):
        has_demo_fields = self.demo_sample_id is not None or self.demo_sample_version is not None
        if self.demo and (self.demo_sample_id is None or self.demo_sample_version is None):
            raise ValueError("演示任务必须带样例标识与版本")
        if not self.demo and has_demo_fields:
            raise ValueError("非演示任务不能带样例标识")
        if self.execution_mode == 'offline' and not self.evaluation_case_id:
            raise ValueError('离线评测任务必须带案例标识')
        if self.execution_mode != 'offline' and (self.evaluation_id or self.evaluation_case_id):
            raise ValueError('评测归属只能用于离线任务')
        return self


def validate_metadata(value) -> dict:
    """严格校验元数据；空值落到空对象，不把未知字段带入库。"""
    data = TaskMetadata.model_validate({} if value is None else value).model_dump()
    # 不向旧样例填充新增的未知元数据。
    return {key: value for key, value in data.items()
            if key not in {'execution_mode', 'evaluation_id', 'evaluation_case_id'} or value is not None}


def is_offline_task(task: dict | None) -> bool:
    """兼容只有系统主题前缀的旧离线任务，明确模式优先。"""
    if task is None:
        return False
    mode = (task.get('metadata') or {}).get('execution_mode')
    return mode == 'offline' or mode is None and task.get('topic', '').startswith('[离线协议] ')


def offline_context_matches(task: dict, context) -> bool:
    """worker 必须持有相同评测和案例的合成依赖。"""
    if context is None:
        return False
    meta = task.get('metadata') or {}
    return (meta.get('execution_mode') == 'offline'
            and meta.get('evaluation_id') == context.evaluation_id
            and meta.get('evaluation_case_id') == context.case['case_id'])
