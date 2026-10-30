"""模型接管契约：当前任务固定备用，新任务才参与主模型恢复试探。"""
from typing import Literal
import os
import asyncio
import hashlib
from datetime import datetime, timezone
from threading import Lock
from time import monotonic

from pydantic import BaseModel, ConfigDict, Field

from services.usage_service import UsagePersistenceError, ModelCallError, measured_call, usage_context
from services.budget import ModelPolicy
from services.execution_context import assert_active, model_deadline


class ModelUnavailable(UsagePersistenceError):
    """模型依赖不可用，Agent不得吞掉后伪造成功，任务可暂停恢复。"""
    code = 'MODEL_UNAVAILABLE'


class ModelRoute(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    task_id: str = Field(min_length=1)
    role: Literal['backup'] = 'backup'
    model: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    reason: Literal['MODEL_TIMEOUT', 'MODEL_CONNECTION', 'MODEL_RATE_LIMIT',
                    'MODEL_AUTH', 'MODEL_SERVICE', 'PRIMARY_CIRCUIT_OPEN']
    switched_at: str = Field(min_length=1)


class FailoverPolicy(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    total_seconds: float = Field(default=120, gt=0, le=1200)
    max_attempts: int = Field(default=2, ge=2, le=6)
    cooldown_seconds: float = Field(default=60, gt=0, le=3600)

    @classmethod
    def from_env(cls):
        try:
            result = cls(total_seconds=os.getenv('MODEL_FAILOVER_TOTAL_SECONDS', '120'),
                         max_attempts=os.getenv('MODEL_FAILOVER_MAX_ATTEMPTS', '2'),
                         cooldown_seconds=os.getenv('MODEL_CIRCUIT_COOLDOWN_SECONDS', '60'))
            for name in ('BACKUP_OPENAI_BASE_URL', 'BACKUP_OPENAI_API_KEY', 'BACKUP_OPENAI_MODEL'):
                if not os.getenv(name, '').strip():
                    raise ValueError
            from urllib.parse import urlsplit
            url = urlsplit(os.environ['BACKUP_OPENAI_BASE_URL'])
            if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password:
                raise ValueError
            return result
        except (ValueError, TypeError):
            raise UsagePersistenceError('主备模型配置无效') from None


class Circuit:
    """同配置共享熔断；冷却后只允许一个真实请求试探。"""
    def __init__(self):
        self.lock = Lock()
        self.until = 0.0
        self.probing = False
        self.generation = 0

    def acquire(self):
        with self.lock:
            if self.until == 0:
                return self.generation
            if monotonic() < self.until or self.probing:
                return None
            self.probing = True
            return self.generation

    def finish(self, ticket, success, cooldown):
        with self.lock:
            if ticket != self.generation:
                return  # 迟到的旧请求不能关闭新一轮熔断。
            if success:
                self.until, self.probing = 0.0, False
            else:
                self.generation += 1
                self.until, self.probing = monotonic() + cooldown, False

    def abandon(self, ticket):
        """控制流错误不证明供应商故障，只释放当前试探资格。"""
        with self.lock:
            if ticket == self.generation:
                self.probing = False


_circuits = {}
_circuits_lock = Lock()


def reset_routing():
    """仅清理进程内健康状态，绝不清除持久化任务绑定。"""
    with _circuits_lock:
        _circuits.clear()


def primary_circuit():
    values = [os.getenv(name, '') for name in ('OPENAI_BASE_URL', 'OPENAI_MODEL', 'OPENAI_API_KEY')]
    key = hashlib.sha256('\0'.join(values).encode()).hexdigest()
    with _circuits_lock:
        return _circuits.setdefault(key, Circuit())


def read_binding(task_id):
    if task_id is None:
        return None
    from db.model_route_repository import get_route
    try:
        return get_route(task_id)
    except Exception:
        raise UsagePersistenceError('任务模型绑定不可读取') from None


def select_backup(task_id, reason):
    route = ModelRoute(task_id=task_id or 'unscoped', model=os.environ['BACKUP_OPENAI_MODEL'],
                       provider=os.getenv('BACKUP_OPENAI_PROVIDER', 'backup') or 'backup', reason=reason,
                       switched_at=datetime.now(timezone.utc).isoformat())
    if task_id is None:
        return route.model_dump()
    from db.model_route_repository import bind_backup
    try:
        return bind_backup(route)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise UsagePersistenceError('备用模型绑定保存失败，已停止调用') from None


async def routed_chat(messages, tools=None, tool_choice=None):
    """主备共享时间/次数/Token预算；模型选择按任务持久化。"""
    from llm.client import get_client, get_backup_client, get_model
    from services.usage_summary import configured_price
    from services.usage_service import classify_model_error
    failover, policy = FailoverPolicy.from_env(), ModelPolicy.from_env()
    assert_active()
    context = usage_context.get()
    task_id = context[0] if context else None
    binding = read_binding(task_id)
    circuit, ticket = primary_circuit(), None
    deadline = monotonic() + failover.total_seconds
    if model_deadline.get() is not None:
        deadline = min(deadline, model_deadline.get())
    if binding is None:
        ticket = circuit.acquire()
        if ticket is None:
            binding = select_backup(task_id, 'PRIMARY_CIRCUIT_OPEN')
    try:
        for attempt in range(1, failover.max_attempts + 1):
            assert_active()
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            role = 'backup' if binding is not None else 'primary'
            timeout = min(policy.timeout_seconds, remaining / 2 if role == 'primary' else remaining)
            attempt_policy = policy.model_copy(update={'timeout_seconds': timeout})
            kwargs = dict(model=binding['model'] if binding else get_model(), messages=messages,
                          max_tokens=policy.completion_reserve, timeout=timeout)
            if tools:
                kwargs['tools'] = tools
            if tool_choice:
                kwargs['tool_choice'] = tool_choice
            price = configured_price(kwargs['model'])
            try:
                try:
                    create = (get_backup_client() if binding else get_client()).chat.completions.create
                except RuntimeError:
                    error = ModelCallError('模型客户端配置不可用')
                    error.failover_reason = 'MODEL_AUTH'
                    raise error from None
                result = await measured_call(create, kwargs, attempt=attempt, policy=attempt_policy, price=price)
            except UsagePersistenceError:
                raise
            except Exception as exc:
                error = exc if isinstance(exc, ModelCallError) else classify_model_error(exc)
                if not error.failover_reason:
                    if role == 'primary':
                        circuit.finish(ticket, True, failover.cooldown_seconds)
                        ticket = None
                    raise error from None
                if role == 'primary':
                    circuit.finish(ticket, False, failover.cooldown_seconds)
                    ticket = None
                    binding = select_backup(task_id, error.failover_reason)
                continue
            else:
                if role == 'primary':
                    circuit.finish(ticket, True, failover.cooldown_seconds)
                    ticket = None
                return result
        raise ModelUnavailable('主备模型暂不可用，已保留任务进度')
    finally:
        if ticket is not None:
            # 取消、预算和持久化错误不能遗留半开试探锁。
            circuit.abandon(ticket)
