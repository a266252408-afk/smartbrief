"""冻结样本的出处、范围及真实规则回放。"""
import hashlib
import json
from pathlib import Path
from services.citations import check_citations
from services.review_contracts import parse_json
from services.fact_binding import build_registry, render_bound_text, BindingError
import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / 'fixtures/review_reliability'


def test_frozen_fixtures_have_unchanged_bytes():
    manifest = json.loads((FIXTURES / 'manifest.json').read_text(encoding='utf-8'))
    assert len(manifest['files']) == 10
    for name, expected in manifest['files'].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name


def test_ddb6_is_backup_evidence_not_primary_success():
    data = json.loads((FIXTURES / 'task_ddb648.json').read_text(encoding='utf-8'))
    assert data['recorded']['quality_status'] == 'rejected'
    assert any(u['model_role'] == 'primary' and u['status'] == 'failed' for u in data['usage'])
    assert {u['model_role'] for u in data['usage'] if u['status'] == 'success'} == {'backup'}
    assert data['route'][0]['role'] == 'backup'


def test_ddb6_replay_uses_full_rewriter_registry_and_saved_responses():
    data = json.loads((FIXTURES / 'task_ddb648.json').read_text(encoding='utf-8'))
    registry = build_registry(data['task_id'], data['evidence'])
    assert len(registry) == 288
    attempts = data['review_history'][0]['rewrite']['attempts']
    assert [a['mode'] for a in attempts] == ['patch', 'full']
    for attempt in attempts:
        assert not attempt['raw_truncated']
        assert hashlib.sha256(attempt['raw_response'].encode()).hexdigest() == attempt['raw_sha256']
    patch = parse_json(attempts[0]['raw_response'])
    assert all('text' in p and 'replacement' not in p for p in patch['patches'])
    full = parse_json(attempts[1]['raw_response'])
    with pytest.raises(BindingError, match='声明与绑定事实不一致|该数值对应的期间与声明不一致'):
        render_bound_text(full['content'], data['task_id'], registry, previous=data['draft_content'])


def test_ddb6_amount_wording_is_normalized_without_rewriting_history():
    data = json.loads((FIXTURES / 'task_ddb648.json').read_text(encoding='utf-8'))
    check = check_citations(data['task_id'], data['draft_content'], data['evidence'])
    assert not check['errors']
    assert not any(i['kind'] == 'unsupported_wording' and '8万元' in i.get('sentence', '') for i in check['binding_issues'])
    assert data['recorded']['quality_status'] == 'rejected'
    assert any('删除 10 条' in log['content'] for log in data['writer_logs'])
    assert data['provenance']['raw_writer_response'] == 'not_persisted'
