"""v3专用合成验收；只替换外部服务，不能证明在线语义能力。"""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.report_document import build_document, fact_context, render_document
from tests.test_evidence import source

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'docs/evaluation-datasets/v3/offline-1.json'


def protocol():
    return json.loads(DATA.read_text(encoding='utf-8'))


def material(case):
    evidence = [source('v3-case', title='合成验收资料', excerpt=case['source_quote'])]
    context = fact_context('v3-case', evidence)
    aliases = {context['registry'][key]['quote']: alias for alias, key in context['aliases'].items()}
    blocks = [dict(kind='fact', segments=[dict(text=quote, facts=[aliases[quote]])])
              for quote in case['source_quote'].splitlines()]
    payload = dict(title=case['topic'], sections=[
        dict(role='summary', title='核心摘要', blocks=[dict(kind='analysis', segments=[dict(text='本报告仅依据合成资料。')])]),
        dict(role='body', title='研究结果', blocks=blocks),
        dict(role='conclusion', title='主要结论', blocks=[dict(kind='recommendation', segments=[dict(text='建议持续核对原始资料。')])])])
    if case.get('id', '').startswith(('V', 'G')) and int(case['id'][1:]) % 2 == 0:
        payload['sections'][1]['blocks'].reverse()
        payload['sections'][0]['title'] = '概要'
    return evidence, context, payload


def test_protocol_is_separate_frozen_and_labeled():
    data = protocol()
    assert data['protocol_id'] == 'v3-offline-1'
    assert data['pipeline_version'] == 3 and data['synthetic'] is True
    assert data['online_status'] == 'pending_authorization'
    rows = data['challenges']
    assert len(rows) == 30 and len({r['id'] for r in rows}) == 30
    assert {k: sum(r['category'] == k for r in rows) for k in ('valid', 'invalid', 'boundary')} == dict(valid=10, invalid=10, boundary=10)
    assert len({r['topic'] for r in rows}) >= 5
    assert {r['split'] for r in rows} == {'development', 'holdout'}
    assert len(data['tasks']) == 15
    assert sum(t['sufficient'] for t in data['tasks']) == 10
    for row in rows + data['tasks']:
        assert row['source_quote'] and row['rationale']
    manifest = json.loads(DATA.with_name('manifest.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(DATA.read_bytes()).hexdigest() == manifest['sha256']


@pytest.mark.parametrize('index', range(30))
def test_challenge_actual_v3_document_boundary(index):
    case = protocol()['challenges'][index]
    evidence, context, payload = material(case)
    if case['category'] == 'valid':
        document = build_document(payload, 'v3-case', evidence, context)
        assert document['version'] == 3
        assert '[S1]' in render_document(document)
        # 等价标题与事实块重排不得改变事实集合。
        changed = copy.deepcopy(payload)
        changed['sections'][1]['blocks'].reverse()
        changed['sections'][0]['title'] = '概要'
        other = build_document(changed, 'v3-case', evidence, context)
        def facts(d):
            return {f for s in d['sections'] for b in s['blocks'] for c in b['claims'] for f in c['fact_ids']}
        assert facts(document) == facts(other)
    else:
        block = payload['sections'][1]['blocks'][0]
        if case['mutation'] == 'source':
            block['segments'][0]['facts'] = ['F99999']
        elif case['mutation'] == 'empty':
            payload['sections'][0]['blocks'] = []
        else:
            block['segments'][0]['text'] = case['replacement']
        with pytest.raises(ValueError):
            build_document(payload, 'v3-case', evidence, context)


@pytest.mark.parametrize('raw,finish', [('', 'stop'), ('{}', 'length')])
def test_empty_and_truncated_responses_cannot_become_valid(raw, finish):
    from services.document_writer import generate_document
    evidence, _, _ = material(protocol()['challenges'][0])
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason=finish)])
    model = AsyncMock(return_value=response)
    with pytest.raises(ValueError):
        asyncio.run(generate_document(model, 'v3-case', '合成测试', evidence))
    assert model.await_count == 2


@pytest.mark.parametrize('index', range(15))
def test_frozen_tasks_real_graph_and_persistence(index, offline_services, monkeypatch):
    from agents import react_researcher
    from db import database, task_repository as repo
    from graph.workflow import run_task
    case = protocol()['tasks'][index]
    _, _, payload = material(case)
    database.init_db()
    task_id = 'v3-case'
    repo.create_task(task_id, case['topic'], metadata={'report_pipeline_version': 3})
    search = dict(results=[dict(title='合成验收资料', url='https://example.invalid/v3', snippet=case['source_quote'])]
                  if case['sufficient'] else [])
    monkeypatch.setattr(react_researcher.mcp_manager, 'call_tool', AsyncMock(return_value=search))
    offline_services['planner'].outputs = [json.dumps([
        dict(id=1, agent='researcher', task='核对资料', depends_on=[]),
        dict(id=2, agent='writer', task='完整报告', depends_on=[1])])]
    offline_services['react_researcher'].outputs = ['{"action":"web_search","query":"合成资料"}', '{"action":"done"}'] * 3
    offline_services['react_writer'].outputs = [json.dumps(payload, ensure_ascii=False)]
    offline_services['reviewer'].outputs = ['{"passed":true,"comments":[]}'] * 3
    if case['sufficient']:
        asyncio.run(run_task(task_id, case['topic']))
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(run_task(task_id, case['topic']))
    saved = repo.get_task(task_id)
    assert saved['metadata']['report_pipeline_version'] == 3
    if case['sufficient']:
        assert saved['quality_status'] == 'passed'
        assert saved['execution_status'] == 'completed'
        assert saved['final_report'] == render_document(saved['report_document'])
        assert saved['evidence'] and saved['review_history']
        assert saved['review_history'][-1]['report_document_sha256']
    else:
        assert saved['quality_status'] != 'passed' and not saved['passed']
        assert not saved['evidence']
        assert not offline_services['react_writer'].calls
    assert repo.get_task(task_id)['quality_status'] == saved['quality_status']


@pytest.mark.parametrize('index', [i for i in range(30) if i % 6 < 4])
def test_challenge_reviewer_does_not_trust_model_passed(index, monkeypatch):
    from agents import reviewer
    from tests.fakes import ScriptedLLM
    case = protocol()['challenges'][index]
    evidence, context, payload = material(case)
    document = build_document(payload, 'v3-case', evidence, context)
    body = render_document(document)
    if case['category'] == 'invalid':
        # v3权威内容是document；污染派生Markdown不能覆盖已验证正文。
        body = body.replace(case['source_quote'].splitlines()[0], case['replacement'])
    model = ScriptedLLM(['{"passed":true,"comments":[]}'] * 3)
    monkeypatch.setattr(reviewer, 'chat', model.chat)
    out = asyncio.run(reviewer.reviewer_node(dict(task_id='v3-case', topic=case['topic'],
        report_pipeline_version=3, report_document=document, draft_content=body, evidence=evidence)))
    assert out['quality_status'] == 'passed'
    if case['category'] == 'invalid':
        damaged = copy.deepcopy(document)
        block = damaged['sections'][1]['blocks'][0]
        block['text'] = case['replacement']
        block['claims'][0].update(quote=case['replacement'], end=len(case['replacement']))
        with pytest.raises(ValueError):
            asyncio.run(reviewer.reviewer_node(dict(task_id='v3-case', topic=case['topic'],
                report_pipeline_version=3, report_document=damaged, draft_content=body, evidence=evidence)))


@pytest.mark.parametrize('mutation', ['year', 'object', 'number', 'negation', 'source'])
def test_opposite_metamorphisms_cannot_preserve_acceptance(mutation):
    case = protocol()['challenges'][0]
    evidence, context, payload = material(case)
    block = payload['sections'][1]['blocks'][0]
    text = block['segments'][0]['text']
    if mutation == 'source':
        block['segments'][0]['facts'] = ['F99999']
    else:
        replacements = {'year': ('2025', '2024'), 'object': ('灌溉设备', '拖拉机'),
                        'number': ('12亿元', '99亿元'), 'negation': ('销售额12亿元', '销售额并非12亿元')}
        old, new = replacements[mutation]
        block['segments'][0]['text'] = text.replace(old, new)
    with pytest.raises(ValueError):
        build_document(payload, 'v3-case', evidence, context)


@pytest.mark.parametrize('index', range(2))
def test_new_holdout_after_scope_fix(index):
    case = json.loads(DATA.with_name('supplement-1.json').read_text(encoding='utf-8'))['cases'][index]
    evidence, context, payload = material(case)
    assert build_document(payload, 'v3-case', evidence, context)['version'] == 3
    for mutation in ('object', 'negation'):
        changed = copy.deepcopy(payload)
        block = changed['sections'][1]['blocks'][0]
        block['segments'][0]['text'] = (
            block['segments'][0]['text'].replace(case['object_from'], case['object_to']) if mutation == 'object'
            else block['segments'][0]['text'].replace('销售额', '销售额并非')
        )
        with pytest.raises(ValueError):
            build_document(changed, 'v3-case', evidence, context)


def test_scope_guard_preserves_negated_original_and_compatible_multi_source():
    from services.report_document import _literal_claim_scope
    # 原句包含否定和多个不同主体时都不能误拒。
    _literal_claim_scope('销售额并非12亿元。', ['销售额并非12亿元。'])
    _literal_claim_scope('2025年中国灌溉设备销售额12亿元。',
                         ['2025年中国拖拉机销售额12亿元。', '2025年中国灌溉设备销售额12亿元。'])
    _literal_claim_scope('资料仅用于讨论，不能据此判断需求。', ['公开资料需要进一步核实。'])
    with pytest.raises(ValueError, match='极性'):
        _literal_claim_scope('销售额12亿元。', ['销售额并非12亿元。'])
