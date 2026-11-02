"""管理员本次明确消费确认；不接受模型密钥或任意供应商配置。"""
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal


class ConsentBase(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    confirmed: bool
    authorization_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')
    dataset_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    estimated_cost_ceiling: float = Field(gt=0)

    @model_validator(mode='after')
    def explicit_confirmation(self):
        if not self.confirmed:
            raise ValueError('需要明确确认本次公开资料发送和消费')
        return self


class ModelConsent(ConsentBase):
    model: str = Field(min_length=1)
    base_url: str
    max_model_calls: int = Field(ge=1, le=40)
    max_input_bytes: int = Field(ge=1, le=200000)
    max_output_tokens: int = Field(ge=1, le=8000)
    input_price_per_million: float = Field(gt=0)
    output_price_per_million: float = Field(gt=0)
    currency: Literal['USD', 'CNY']


class SearchConsent(ConsentBase):
    per_call_estimate_ceiling: float = Field(gt=0)
