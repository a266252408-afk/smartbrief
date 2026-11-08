"""思考档位在业务主备与独立评测的供应商边界生效；不调用真实模型。"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AuthenticationError

from services.model_reasoning import reasoning_options
from services.usage_service import UsagePersistenceError, usage_scope


@pytest.mark.parametrize('effort', ['low', 'high', 'max'])
@pytest.mark.parametrize('model', ['GLM-5.3-Flash', 'glm-5.3', 'zai-org/GLM-5.3-Flash'])
def test_configured_glm_effort(effort, model, monkeypatch):
    monkeypatch.setenv('MODEL_REASONING_EFFORT', effort)
    assert reasoning_options(model) == {'reasoning_effort': effort}


def test_default_high_and_models_without_documented_levels(monkeypatch):
    monkeypatch.delenv('MODEL_REASONING_EFFORT', raising=False)
    assert reasoning_options('GLM-5.3-Flash') == {'reasoning_effort': 'high'}
    for model in ('qwen3.7-flash', 'gpt-4o-mini', 'fixture', 'glm-4.7', 'glm-5.30'):
        assert reasoning_options(model) == {}


@pytest.mark.parametrize('value', ['', 'medium', 'maxx'])
def test_invalid_effort_stops_before_supplier(value, monkeypatch):
    from db import database
    from llm import client
    database.init_db()
    monkeypatch.setenv('MODEL_REASONING_EFFORT', value)
    monkeypatch.setenv('OPENAI_MODEL', 'GLM-5.3-Flash')
    create = AsyncMock()
    monkeypatch.setattr(client, 'get_client', lambda: sdk(create))
    with pytest.raises(UsagePersistenceError, match='MODEL_REASONING_EFFORT'):
        asyncio.run(client.chat([{'role': 'user', 'content': '测试'}]))
    create.assert_not_awaited()


def sdk(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.mark.parametrize('role', [None, 'review', 'rewrite'])
def test_direct_calls_use_editable_effort(role, monkeypatch):
    from db import database
    from llm import client
    database.init_db()
    monkeypatch.setenv('OPENAI_MODEL', 'GLM-5.3-Flash')
    monkeypatch.setenv('REVIEW_OPENAI_MODEL', 'GLM-5.3-Flash')
    monkeypatch.setenv('REWRITE_OPENAI_MODEL', 'GLM-5.3-Flash')
    create = AsyncMock(return_value=SimpleNamespace(usage=None))
    monkeypatch.setattr(client, 'get_client', lambda: sdk(create))
    for effort in ('high', 'low'):
        monkeypatch.setenv('MODEL_REASONING_EFFORT', effort)
        asyncio.run(client.chat([{'role': 'user', 'content': '测试'}], role=role))
        assert create.call_args.kwargs['reasoning_effort'] == effort


def test_primary_and_backup_share_effort(monkeypatch):
    from db import database
    from llm import client
    from services.model_routing import reset_routing
    database.init_db()
    reset_routing()
    monkeypatch.setenv('MODEL_FAILOVER_ENABLED', 'true')
    monkeypatch.setenv('OPENAI_MODEL', 'glm-5.3')
    monkeypatch.setenv('BACKUP_OPENAI_MODEL', 'GLM-5.3-Flash')
    monkeypatch.setenv('BACKUP_OPENAI_API_KEY', 'fixture')
    monkeypatch.setenv('BACKUP_OPENAI_BASE_URL', 'https://backup.invalid/v1')
    monkeypatch.setenv('MODEL_REASONING_EFFORT', 'high')
    primary = AsyncMock(side_effect=AuthenticationError('fixture',
        response=httpx.Response(401, request=httpx.Request('POST', 'https://primary.invalid')), body=None))
    backup = AsyncMock(return_value=SimpleNamespace(usage=None))
    monkeypatch.setattr(client, 'get_client', lambda: sdk(primary))
    monkeypatch.setattr(client, 'get_backup_client', lambda: sdk(backup))
    @usage_scope('writer', task=True)
    async def invoke(task_id):
        return await client.chat([{'role': 'user', 'content': '测试'}])
    asyncio.run(invoke('reasoning-failover'))
    assert primary.call_args.kwargs['reasoning_effort'] == 'high'
    assert backup.call_args.kwargs['reasoning_effort'] == 'high'


def test_evaluation_uses_authorized_model_and_global_effort(monkeypatch):
    from evaluation.authorization import CallBudget
    monkeypatch.setenv('MODEL_REASONING_EFFORT', 'low')
    grant = SimpleNamespace(model='GLM-5.3-Flash', max_model_calls=1, max_input_bytes=1000,
        max_output_tokens=100, expires_at=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat())
    create = AsyncMock()
    budget = CallBudget(grant, create)
    asyncio.run(budget(messages=[{'role': 'user', 'content': '测试'}], model='other', reasoning_effort='max'))
    assert create.call_args.kwargs['model'] == grant.model
    assert create.call_args.kwargs['reasoning_effort'] == 'low'
    assert create.call_args.kwargs['max_tokens'] == 100
