"""评测调用授权契约；网页不能签发授权。"""
from typing import Literal
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field
from db import evaluation_run_repository as repository
from evaluation import catalog
from services.evaluation_service import EvaluationError


class EvaluationGrant(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    authorization_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')
    dataset_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    case_ids: list[str] = Field(min_length=1, max_length=4)
    model: str = Field(min_length=1)
    base_url: str
    expires_at: str
    max_model_calls: int = Field(ge=1, le=40)
    max_input_bytes: int = Field(ge=1, le=200000)
    max_output_tokens: int = Field(ge=1, le=8000)
    input_price_per_million: float = Field(gt=0)
    output_price_per_million: float = Field(gt=0)
    currency: Literal['USD', 'CNY']
    estimated_cost_ceiling: float = Field(gt=0)


class SearchEvaluationGrant(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    authorization_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')
    dataset_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    case_ids: list[Literal['G001', 'G003', 'G005', 'G007', 'G009', 'G011', 'G013', 'G015']] = Field(min_length=1, max_length=8)
    recipient: Literal['https://api.exa.ai/search'] = 'https://api.exa.ai/search'
    expires_at: str
    max_search_calls: int = Field(ge=1, le=8)
    per_call_estimate_ceiling: float = Field(gt=0)
    estimated_cost_ceiling: float = Field(gt=0)
    currency: Literal['USD'] = 'USD'
    search_type: Literal['auto'] = 'auto'
    max_results: Literal[5] = 5


# 完整任务串行执行且单次调用上限 90 秒，30 分钟不足以覆盖 4 例；调用次数与费用上限仍独立生效。
FULL_TASK_GRANT_MINUTES = 120
# 实测两轮研究、写作、审核与补充研究/改写需要 12 次以上；10 次会在改写前耗尽。
FULL_TASK_MIN_MODEL_CALLS = 20


def _grant_minutes(body):
    return FULL_TASK_GRANT_MINUTES if getattr(body, 'profile', None) == 'full_task_live' else 30


def reserve(body):
    try:
        consent = getattr(body, 'live_consent', None)
        grant = (EvaluationGrant(**consent.model_dump(exclude={'confirmed'}),case_ids=body.case_ids,
            expires_at=(datetime.now(timezone.utc)+timedelta(minutes=_grant_minutes(body))).isoformat()) if consent else
            EvaluationGrant.model_validate_json(os.environ.get('SMARTBRIEF_EVALUATION_GRANT', '')))
        requested_id = consent.authorization_id if consent else body.authorization_id
        endpoint = urlsplit(grant.base_url)
        estimate = len(body.case_ids) * grant.max_model_calls * (
            grant.max_input_bytes * grant.input_price_per_million + grant.max_output_tokens * grant.output_price_per_million) / 1000000
        if (requested_id != grant.authorization_id or body.group != 'primary'
                or body.case_ids != grant.case_ids or grant.dataset_hash != catalog.summary()['dataset_hash']
                or datetime.fromisoformat(grant.expires_at) <= datetime.now(timezone.utc)
                or endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                or grant.base_url != os.getenv('OPENAI_BASE_URL') or grant.model != os.getenv('OPENAI_MODEL')
                or not os.getenv('OPENAI_API_KEY') or estimate > grant.estimated_cost_ceiling):
            raise ValueError
    except (ValueError, TypeError):
        raise EvaluationError('LIVE_EVALUATION_NOT_AUTHORIZED', '需单独批准固定资料去向、模型、调用量、预算与有效期；本次未启动', 403) from None
    if grant.max_model_calls < FULL_TASK_MIN_MODEL_CALLS:
        raise EvaluationError('LIVE_EVALUATION_BUDGET_TOO_LOW',
            f'完整任务每案例至少需要 {FULL_TASK_MIN_MODEL_CALLS} 次模型调用；当前 {grant.max_model_calls} 次会在改写前耗尽，本次未启动', 403)
    _reserve_once(grant, estimate)
    return grant


def reserve_search(body):
    try:
        consent = getattr(body, 'live_consent', None)
        grant = (SearchEvaluationGrant(**consent.model_dump(exclude={'confirmed'}),case_ids=body.case_ids,
            max_search_calls=len(body.case_ids),expires_at=(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()) if consent else
            SearchEvaluationGrant.model_validate_json(os.environ.get('SMARTBRIEF_SEARCH_EVALUATION_GRANT', '')))
        requested_id = consent.authorization_id if consent else body.authorization_id
        estimate = grant.max_search_calls * grant.per_call_estimate_ceiling
        if (requested_id != grant.authorization_id or body.case_ids != grant.case_ids
                or grant.dataset_hash != catalog.summary()['dataset_hash']
                or datetime.fromisoformat(grant.expires_at) <= datetime.now(timezone.utc)
                or not os.getenv('EXA_API_KEY', '').strip() or len(body.case_ids) > grant.max_search_calls
                or estimate > grant.estimated_cost_ceiling):
            raise ValueError
    except (ValueError, TypeError):
        raise EvaluationError('LIVE_SEARCH_NOT_AUTHORIZED', '真实搜索需单独批准公开查询去向、调用数、估计费用和有效期；本次未启动', 403) from None
    _reserve_once(grant, estimate)
    return grant


def _reserve_once(grant, estimate):
    # 单次预留采用排他创建；模型与搜索不能重复使用同一授权编号。
    path = repository.root() / 'authorizations' / (hashlib.sha256(grant.authorization_id.encode()).hexdigest() + '.json')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise EvaluationError('EVALUATION_GRANT_STORAGE', '授权预留失败，已停止调用') from None
    try:
        with path.open('x', encoding='utf8') as file:
            json.dump(dict(grant=grant.model_dump(), estimated_reservation=estimate), file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
    except FileExistsError:
        raise EvaluationError('EVALUATION_GRANT_USED', '此授权已预留，不能重复付费运行', 409) from None
    except OSError:
        raise EvaluationError('EVALUATION_GRANT_STORAGE', '授权预留失败，已停止调用') from None


class CallBudget:
    def __init__(self, grant, create):
        self.grant, self.create, self.calls = grant, create, 0

    async def __call__(self, **kwargs):
        payload = {k: kwargs[k] for k in ('messages', 'tools', 'tool_choice') if k in kwargs}
        size = len(json.dumps(payload, ensure_ascii=False).encode('utf8'))
        if (self.calls >= self.grant.max_model_calls or size > self.grant.max_input_bytes
                or datetime.fromisoformat(self.grant.expires_at) <= datetime.now(timezone.utc)):
            raise EvaluationError('EVALUATION_CALL_BUDGET', '评测调用次数或输入预算耗尽')
        from services.model_reasoning import reasoning_options
        kwargs.update(max_tokens=self.grant.max_output_tokens, model=self.grant.model,
                      **reasoning_options(self.grant.model))
        self.calls += 1
        return await self.create(**kwargs)
