"""运行路径隔离与实际调用方重载检查。"""
import importlib
import os
import subprocess
import sys

import pytest

import data_paths


def test_default_keeps_local_data(monkeypatch):
    monkeypatch.delenv('DATA_DIR', raising=False)
    assert data_paths.data_root() == data_paths.BACKEND_DIR
    assert data_paths.runtime_path('data.db') == data_paths.BACKEND_DIR / 'data.db'


def test_explicit_root_and_invalid_paths(tmp_path, monkeypatch):
    root = tmp_path / '独立数据'
    monkeypatch.setenv('DATA_DIR', str(root))
    assert data_paths.runtime_path('data/uploads/file.txt') == root / 'data/uploads/file.txt'
    assert (root / 'data/uploads').is_dir()
    for value in ('../escape', '/absolute', 'C:\\escape', 'data/../../escape'):
        with pytest.raises(ValueError):
            data_paths.runtime_path(value)
    for value in ('relative', ''):
        monkeypatch.setenv('DATA_DIR', value)
        with pytest.raises(ValueError, match='绝对路径'):
            data_paths.data_root()


def test_actual_consumers_share_root_and_seeds_stay_in_code(tmp_path, monkeypatch):
    modules = {
        'db.database': {'DB_PATH': 'data.db'},
        'memory.checkpointer': {'CHECKPOINT_DB': 'checkpoints.db'},
        'rag.store': {'CHROMA_PERSIST_DIR': 'data/chroma_db', 'UPLOADS_DIR': 'data/uploads'},
        'tools.builtin_tools': {'CONFIG_PATH': 'tool_config.json'},
        'mcp_client.mcp_client': {'CONFIG_PATH': 'mcp_config.json'},
        'skills.skills_manager': {'SKILL_CONFIG_PATH': 'skill_config.json', 'SKILLS_POOL_DIR': 'skills_pool'},
    }
    # 保存原属性，避免importlib.reload绕过autouse的monkeypatch回滚。
    originals = {name: dict(vars(importlib.import_module(name))) for name in modules}
    monkeypatch.setenv('DATA_DIR', str(tmp_path / 'volume'))
    try:
        for name, fields in modules.items():
            module = importlib.reload(importlib.import_module(name))
            for attr, relative in fields.items():
                assert getattr(module, attr) == tmp_path / 'volume' / relative
        rag = importlib.import_module('rag.store')
        assert rag.TEMPLATES_DIR == data_paths.BACKEND_DIR / 'data/templates'
        assert rag.TERMINOLOGIES_FILE.is_file()
    finally:
        for name, attributes in originals.items():
            module = importlib.import_module(name)
            vars(module).update(attributes)


def test_root_file_propagates_failure(tmp_path, monkeypatch):
    path = tmp_path / 'file'
    path.write_text('文件', 'utf-8')
    monkeypatch.setenv('DATA_DIR', str(path))
    with pytest.raises(OSError):
        data_paths.runtime_path('data.db')


def test_independent_processes_persist_complete_data(tmp_path):
    env = dict(os.environ, DATA_DIR=str(tmp_path / 'volume'))
    script = data_paths.BACKEND_DIR / 'tests/fixtures/data_roundtrip.py'
    for mode in ('write', 'read', 'read'):
        result = subprocess.run([sys.executable, str(script), mode], env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'DATA_ROUNDTRIP_' + mode.upper() + '_PASSED' in result.stdout
