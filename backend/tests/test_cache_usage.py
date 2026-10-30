"""缓存Token捕获、供应商差异、未知值与费用快照。"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from db import database, task_repository, usage_repository
from llm import client
from services.usage_service import usage_scope, ModelUsage
from services.usage_summary import task_usage, ModelPrice, apply_price


@pytest.fixture
def model(monkeypatch):
    database.init_db()
    task_repository.create_task('cache', '缓存计量')
    create = AsyncMock()
    monkeypatch.setattr(client, 'get_client', lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setenv('OPENAI_MODEL', 'fixture')
    return create


@usage_scope('writer', task=True)
async def invoke(task_id='cache'):
    await client.chat([])


@pytest.mark.parametrize('details,expected', [
    ({'prompt_tokens_details':{'cached_tokens':80,'cache_write_tokens':10}}, (80,10)),
    ({'prompt_tokens_details':SimpleNamespace(cached_tokens=0)}, (0,None)),
    ({'prompt_cache_hit_tokens':70,'prompt_cache_miss_tokens':30}, (70,None)),
    ({}, (None,None)),
    ({'prompt_tokens_details':{'cached_tokens':True}}, (None,None)),
    ({'prompt_tokens_details':{'cached_tokens':-1}}, (None,None)),
    ({'prompt_tokens_details':{'cached_tokens':101}}, (None,None)),
    ({'prompt_cache_hit_tokens':70,'prompt_cache_miss_tokens':40}, (None,None)),
    ({'prompt_tokens_details':{'cached_tokens':80},'prompt_cache_hit_tokens':70}, (None,None)),
])
def test_capture_cache_fields_without_guessing(model, details, expected):
    model.return_value = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, **details))
    asyncio.run(invoke('cache'))
    row = usage_repository.list_usage('cache')[0]
    assert (row['cached_input_tokens'], row['cache_write_tokens']) == expected
    assert row['input_tokens'] == 100
    assert task_usage('cache')['budget_charged_tokens'] == 120


def test_cache_price_does_not_double_count_input_and_is_historical(model, monkeypatch):
    monkeypatch.setenv('MODEL_PRICES_JSON', json.dumps({'primary:fixture':dict(
        input_per_million=2, output_per_million=4, cache_read_per_million=0.5,
        cache_write_per_million=3, currency='USD')}))
    model.return_value = SimpleNamespace(usage={'prompt_tokens':100,'completion_tokens':20,
                                               'prompt_tokens_details':{'cached_tokens':80,'cache_write_tokens':10}})
    asyncio.run(invoke('cache'))
    monkeypatch.setenv('MODEL_PRICES_JSON', '{}')
    row = usage_repository.list_usage('cache')[0]
    assert row['estimated_cost'] == pytest.approx((10*2 + 80*0.5 + 10*3 + 20*4)/1000000)
    assert row['provider'] == 'primary' and row['model_role'] == 'primary' and row['logical_call_id']
    report = task_usage('cache')
    assert report['cached_input_tokens'] == 80 and report['cache_write_tokens'] == 10
    assert report['cache_unknown_calls'] == 0 and report['cost_complete']


def test_additional_write_fee_and_unknown_required_fields():
    base = dict(call_id='c',task_id='t',agent='writer',model='m',status='success',duration_ms=0.0,
                created_at='fixture',input_tokens=100,output_tokens=20,usage_known=True)
    price = ModelPrice(input_per_million=2.0,output_per_million=4.0,cache_read_per_million=0.5,
                       cache_write_per_million=3.0,cache_write_mode='additional',currency='USD')
    row = ModelUsage(**base,cached_input_tokens=80,cache_write_tokens=10)
    apply_price(row,price)
    assert row.estimated_cost == pytest.approx((20*2 + 80*0.5 + 10*3 + 20*4)/1000000)
    for counters in ({}, {'cached_input_tokens':80}, {'cache_write_tokens':10}):
        unknown = ModelUsage(**base,**counters)
        apply_price(unknown,price)
        assert unknown.estimated_cost is None


def test_missing_cache_price_and_unknown_usage_cannot_claim_total(model, monkeypatch):
    monkeypatch.setenv('MODEL_PRICES_JSON','{"fixture":{"input_per_million":2,"output_per_million":4,"currency":"USD"}}')
    for details in ({}, {'cached_tokens':80}, {'cached_tokens':0,'cache_write_tokens':10}):
        model.return_value = SimpleNamespace(usage={'prompt_tokens':100,'completion_tokens':20,
                                                   'prompt_tokens_details':details})
        asyncio.run(invoke('cache'))
    report = task_usage('cache')
    assert report['cached_input_tokens'] is None and report['cache_unknown_calls'] == 1
    assert not report['cost_complete'] and report['estimated_costs'] == {}


def test_old_usage_retains_cost_and_unknown_cache():
    database.init_db()
    task_repository.create_task('cache','历史')
    old = dict(call_id='old',task_id='cache',agent='writer',model='old',status='success',duration_ms=0.0,
               created_at='fixture',input_tokens=10,output_tokens=5,usage_known=True,estimated_cost=0.01,currency='USD')
    with database.get_connection() as conn:
        conn.execute('INSERT INTO model_usage VALUES (?,?,?)',('old','cache',json.dumps(old)))
    report = task_usage('cache')
    assert report['cached_input_tokens'] is None and report['cache_unknown_calls'] == 1
    assert report['estimated_costs'] == {'USD':0.01}


def test_usage_api_exposes_backup_and_only_recovery_projection(model):
    from fastapi.testclient import TestClient
    from main import app
    from tests.auth_support import READ_HEADERS
    from services.model_routing import ModelRoute
    from db.model_route_repository import bind_backup
    bind_backup(ModelRoute(task_id='cache',model='backup',provider='fixture',reason='MODEL_CONNECTION',switched_at='fixture'))
    data = TestClient(app).get('/api/v2/tasks/cache/usage',headers=READ_HEADERS).json()['data']
    assert data['routing']['role'] == 'backup' and data['recovery'] is None
    assert 'api_key' not in json.dumps(data) and 'messages' not in json.dumps(data)


def test_real_checkpoint_projection_tracks_agent_boundaries(offline_evidence):
    from graph import workflow
    from memory import checkpointer
    scripts = offline_evidence
    scripts['planner'].outputs = ['[{"id":1,"agent":"researcher","task":"研究","depends_on":[]},{"id":2,"agent":"writer","task":"PRIVATE_CHAPTER","depends_on":[1]}]']
    scripts['react_writer'].outputs = ['## PRIVATE_DRAFT[S1]']
    scripts['reviewer'].outputs = ['{"passed":true,"comments":[]}']
    config = {'configurable':{'thread_id':'position'}}
    async def scenario():
        async with checkpointer.open_checkpointer() as (cp,_):
            graph = workflow.build_graph(checkpointer=cp)
            await graph.ainvoke(workflow._initial_state('position','fixture'),config,interrupt_after=['scheduler'])
        projected = checkpointer.recovery_position('position')
        assert projected == dict(resume_mode='checkpoint',next_agent='writer',next_step_id=2,completed_steps=[1])
        assert 'PRIVATE' not in json.dumps(projected)
        assert checkpointer.recovery_position('missing') is None
        async with checkpointer.open_checkpointer() as (cp,_):
            await workflow.build_graph(checkpointer=cp,interrupt_before=['reviewer']).ainvoke(None,config)
        assert checkpointer.recovery_position('position')['next_agent'] == 'reviewer'
        async with checkpointer.open_checkpointer() as (cp,_):
            await workflow.build_graph(checkpointer=cp).ainvoke(None,config)
        assert checkpointer.recovery_position('position')['resume_mode'] == 'restart'
        assert checkpointer.recovery_position('position')['next_agent'] is None
    asyncio.run(scenario())


def test_corrupt_checkpoint_projection_is_unknown():
    from memory import checkpointer
    checkpointer.CHECKPOINT_DB.write_bytes(b'corrupt-fixture')
    assert checkpointer.recovery_position('broken') is None
    assert checkpointer.CHECKPOINT_DB.read_bytes() == b'corrupt-fixture'


@pytest.mark.parametrize('counters', [
    {'cached_input_tokens':101}, {'cached_input_tokens':True}, {'cache_write_tokens':-1},
    {'cached_input_tokens':1,'usage_known':False,'input_tokens':None,'output_tokens':None},
])
def test_invalid_persisted_cache_counts_are_rejected(counters):
    base = dict(call_id='c',task_id='t',agent='writer',model='m',status='success',duration_ms=0.0,
                created_at='fixture',input_tokens=100,output_tokens=20,usage_known=True)
    with pytest.raises(ValueError):
        ModelUsage(**(base | counters))


def test_cache_totals_and_zero_match_public_contract(model):
    from pathlib import Path
    import jsonschema
    model.return_value = SimpleNamespace(usage={'prompt_tokens':0,'completion_tokens':0,
                                               'prompt_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}})
    asyncio.run(invoke('cache'))
    report = task_usage('cache')
    assert report['cached_input_tokens'] == report['cache_write_tokens'] == 0
    schema = json.loads((Path(__file__).resolve().parents[2] / 'docs/schemas/contracts.json').read_text('utf-8'))
    jsonschema.validate(report,dict(schema,**{'$ref':'#/$defs/UsageReport'}))
