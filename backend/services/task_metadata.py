"""任务元数据：生成版本与显式演示标识；演示任务必须可追溯样例版本。"""
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator, model_validator


class TaskMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    demo: StrictBool = False
    generator_version: str | None = None
    demo_sample_id: str | None = None
    demo_sample_version: str | None = None

    @field_validator("generator_version", "demo_sample_id", "demo_sample_version")
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
        return self


def validate_metadata(value) -> dict:
    """严格校验元数据；空值落到空对象，不把未知字段带入库。"""
    return TaskMetadata.model_validate({} if value is None else value).model_dump()
