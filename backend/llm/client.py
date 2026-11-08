"""大模型封装：OpenAI 兼容接口。

配置从 `backend/.env` 读取（示例见同目录 .env.example）：
- OPENAI_BASE_URL：接口地址（默认 https://api.openai.com/v1）
- OPENAI_API_KEY：API Key（必填）
- OPENAI_MODEL：模型名（默认 gpt-4o-mini）

实现说明：`load_dotenv()` 默认从**当前工作目录**找 .env，
后端以 backend/ 为启动目录运行（uvicorn main:app），
因此实际加载的就是 backend/.env。
"""
import os
from uuid import uuid4

from dotenv import load_dotenv
from openai import AsyncOpenAI
from services.usage_service import measured_call, ModelCallError
from services.budget import ModelPolicy, completion_reserve_for

# 加载 backend/.env（load_dotenv 默认读当前工作目录的 .env，无副作用可重复调用）
load_dotenv()

_client: AsyncOpenAI | None = None
_backup_client: AsyncOpenAI | None = None


def get_backup_client() -> AsyncOpenAI:
    """备用使用独立地址与密钥；仅接管时懒加载。"""
    global _backup_client
    if _backup_client is None:
        _backup_client = AsyncOpenAI(base_url=os.environ['BACKUP_OPENAI_BASE_URL'],
                                    api_key=os.environ['BACKUP_OPENAI_API_KEY'], max_retries=0)
    return _backup_client


def get_client() -> AsyncOpenAI:
    """获取（懒加载）AsyncOpenAI 客户端。"""
    global _client
    if _client is None:
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("未配置 OPENAI_API_KEY，请在 backend/.env 中填写")
        _client = AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=0)
    return _client


_ROLE_MODEL_ENV = {"review": "REVIEW_OPENAI_MODEL", "rewrite": "REWRITE_OPENAI_MODEL"}


def get_model(role: str | None = None) -> str:
    """当前使用的模型名；review/rewrite 可用独立环境变量覆盖，未配置时回退主模型。"""
    from evaluation.context import evaluation_context
    context = evaluation_context.get()
    if context is not None:
        return context.model
    env_key = _ROLE_MODEL_ENV.get(role or "")
    if env_key:
        configured = os.getenv(env_key, "").strip()
        if configured:
            return configured
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")


async def chat(messages: list, tools: list | None = None, tool_choice: str | None = None,
               role: str | None = None, output_contract=None):
    """调用 Chat Completions。

    :param messages: OpenAI 格式的消息列表
    :param tools: OpenAI 格式的工具 Schema 列表（Function Calling）
    :param tool_choice: 可选 "auto" / "none"
    :param role: review / rewrite 时读取独立模型配置；不影响主备路由契约
    :return: ChatCompletion 响应对象（含 content / tool_calls）
    """
    from evaluation.context import evaluation_context
    context = evaluation_context.get()
    model = get_model(role)
    if context is None and os.getenv('MODEL_FAILOVER_ENABLED', 'false').lower() == 'true':
        from services.model_routing import routed_chat
        return await routed_chat(messages, tools, tool_choice, model=model, output_contract=output_contract)
    create = context.create if context is not None else get_client().chat.completions.create
    policy = (ModelPolicy(max_task_tokens=1 if context.case['scenario'] == 'budget' else 200000,
                          completion_reserve=2048, timeout_seconds=10, max_retries=0)
              if context is not None else ModelPolicy.for_role('primary'))
    from services.usage_summary import configured_price
    provider = 'offline' if context is not None else os.getenv('OPENAI_PROVIDER', 'primary') or 'primary'
    price = None if context is not None else configured_price(model, provider)
    logical_call_id = uuid4().hex
    from services.structured_output import prepare_output
    messages, output_kwargs, output_meta = prepare_output(messages, output_contract, model, os.getenv('OPENAI_BASE_URL'))
    kwargs = {"model": model, "messages": messages, **output_kwargs}
    if tools:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    kwargs["max_tokens"] = completion_reserve_for(model, policy)
    kwargs["timeout"] = policy.timeout_seconds
    for attempt in range(1, policy.max_retries + 2):
        try:
            return await measured_call(create, kwargs, attempt=attempt, policy=policy, price=price,
                                       provider=provider, model_role=None if context is not None else 'primary',
                                       logical_call_id=logical_call_id)
        except ModelCallError as exc:
            if not exc.retryable or attempt > policy.max_retries:
                raise
