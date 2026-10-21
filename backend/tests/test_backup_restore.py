"""真实备份/副本恢复及失败边界；用户数据不参与。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import sqlite3

import pytest

from data_paths import BACKEND_DIR
from db import database
from services.data_backup import create_backup, restore_backup
from services.instance_lock import InstanceLock
from services import data_backup as service

TAG = 'sb-T10-01-verified'


@pytest.fixture
def complete_data(tmp_path):
    source = tmp_path / 'source'
    env = dict(os.environ, DATA_DIR=str(source))
    script = BACKEND_DIR / 'tests/fixtures/data_roundtrip.py'
    result = subprocess.run([sys.executable, str(script), 'write'], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return source


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_complete_backup_restore_and_read_in_new_process(complete_data, tmp_path):
    source = complete_data
    # 测试夹具expected.json不属于业务备份，把上传ID从真实索引查回。
    (source / '.env').write_text('FIXTURE_SECRET=must-not-copy', 'utf-8')
    backup, target = tmp_path / 'backup', tmp_path / 'restored'
    manifest = create_backup(source, backup, TAG, stopped=True)
    assert manifest.data_schema_version == database.SCHEMA_VERSION
    assert manifest.consistency == 'stopped_with_instance_lock'
    assert not (backup / '.env').exists()
    assert not (backup / 'expected.json').exists()
    assert manifest.checks['sqlite']['data.db']['rows']['tasks'] == 1
    assert {x['name']: x['count'] for x in manifest.checks['collections']} == {'memory_collection': 1, 'rag_collection': 1}
    for entry in manifest.files:
        assert digest(source / entry.path) == digest(backup / entry.path) == entry.sha256
    restore_backup(backup, target, TAG)
    for entry in manifest.files:
        assert digest(target / entry.path) == entry.sha256
    # 外部比较夹具显式置入恢复目录，仅帮助断言实际上传ID，不伪造业务数据。
    (target / 'expected.json').write_bytes((source / 'expected.json').read_bytes())
    result = subprocess.run([sys.executable, str(BACKEND_DIR / 'tests/fixtures/data_roundtrip.py'), 'read'], env=dict(os.environ, DATA_DIR=str(target)), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'DATA_ROUNDTRIP_READ_PASSED' in result.stdout


def test_backup_requires_stopped_and_rejects_active_instance(tmp_path):
    source = tmp_path / 'source'
    database.init_db(source / 'data.db')
    with pytest.raises(ValueError, match='停'):
        create_backup(source, tmp_path / 'backup', TAG)
    lock = InstanceLock(source / 'data.runner.lock')
    lock.acquire()
    try:
        with pytest.raises(RuntimeError):
            create_backup(source, tmp_path / 'backup', TAG, stopped=True)
        assert not (tmp_path / 'backup').exists()
    finally:
        lock.release()


def test_paths_hashes_version_and_code_mismatch(tmp_path):
    source = tmp_path / 'source'
    database.init_db(source / 'data.db')
    for target in (source / 'inside', source, Path('relative')):
        with pytest.raises(ValueError):
            create_backup(source, target, TAG, stopped=True)
    backup = tmp_path / 'backup'
    create_backup(source, backup, TAG, stopped=True)
    with pytest.raises(ValueError, match='代码'):
        restore_backup(backup, tmp_path / 'wrong-code', 'old-code')
    target = tmp_path / 'exists'
    target.mkdir()
    with pytest.raises((ValueError, FileExistsError)):
        restore_backup(backup, target, TAG)
    (backup / 'data.db').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='哈希|大小'):
        restore_backup(backup, tmp_path / 'corrupt', TAG)
    assert not (tmp_path / 'corrupt').exists()


@pytest.mark.parametrize('change', ['traversal', 'extra', 'missing', 'future', 'duplicate', 'secret', 'version_mismatch'])
def test_manifest_and_inventory_rejection(tmp_path, change):
    source = tmp_path / 'source'
    database.init_db(source / 'data.db')
    backup = tmp_path / 'backup'
    create_backup(source, backup, TAG, stopped=True)
    manifest_path = backup / 'manifest.json'
    data = json.loads(manifest_path.read_text('utf-8'))
    if change == 'traversal':
        data['files'][0]['path'] = '../escape'
    elif change == 'extra':
        (backup / 'extra.txt').write_text('unexpected')
    elif change == 'missing':
        (backup / 'data.db').unlink()
    elif change == 'future':
        data['data_schema_version'] = 999
    elif change == 'duplicate':
        data['files'].append(data['files'][0])
    elif change == 'secret':
        data['files'][0]['path'] = '.env'
    elif change == 'version_mismatch':
        data['data_schema_version'] = 1
    manifest_path.write_text(json.dumps(data), 'utf-8')
    with pytest.raises(ValueError):
        restore_backup(backup, tmp_path / 'target', TAG)
    assert not (tmp_path / 'target').exists()


def test_invalid_source_configuration_and_copy_failure(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='源数据'):
        create_backup(tmp_path / 'missing', tmp_path / 'out', TAG, stopped=True)
    source = tmp_path / 'source'
    source.mkdir()
    with pytest.raises(ValueError, match='业务库'):
        create_backup(source, tmp_path / 'empty', TAG, stopped=True)
    database.init_db(source / 'data.db')
    with pytest.raises(ValueError, match='标签'):
        create_backup(source, tmp_path / 'no-code', '', stopped=True)
    (source / 'tool_config.json').write_text('[]', 'utf-8')
    with pytest.raises(ValueError, match='JSON对象'):
        create_backup(source, tmp_path / 'bad-config', TAG, stopped=True)
    assert not (tmp_path / 'bad-config/manifest.json').exists()
    (source / 'tool_config.json').write_text('{}', 'utf-8')
    def fail(*args, **kwargs):
        raise OSError('固定磁盘故障')
    monkeypatch.setattr(service.shutil, 'copy2', fail)
    with pytest.raises(OSError, match='固定磁盘故障'):
        create_backup(source, tmp_path / 'copy-failed', TAG, stopped=True)
    assert not (tmp_path / 'copy-failed/manifest.json').exists()


def test_source_change_and_manifest_business_mismatch(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    database.init_db(source / 'data.db')
    original = service._copy
    def change(source_dir, target_dir, files):
        original(source_dir, target_dir, files)
        if source_dir == source:
            (source / 'skill_config.json').write_text('{}', 'utf-8')
    monkeypatch.setattr(service, '_copy', change)
    with pytest.raises(ValueError, match='源数据发生变化'):
        create_backup(source, tmp_path / 'changing', TAG, stopped=True)
    assert not (tmp_path / 'changing/manifest.json').exists()
    monkeypatch.setattr(service, '_copy', original)
    backup = tmp_path / 'backup'
    create_backup(source, backup, TAG, stopped=True)
    path = backup / 'manifest.json'
    manifest = json.loads(path.read_text('utf-8'))
    manifest['checks']['sqlite']['data.db']['rows']['tasks'] = 123
    path.write_text(json.dumps(manifest), 'utf-8')
    with pytest.raises(ValueError, match='行数'):
        restore_backup(backup, tmp_path / 'mismatch', TAG)


def test_future_and_corrupt_source_database(tmp_path):
    source = tmp_path / 'source'
    database.init_db(source / 'data.db')
    with sqlite3.connect(source / 'data.db') as conn:
        conn.execute('PRAGMA user_version=999')
    with pytest.raises(ValueError, match='版本'):
        create_backup(source, tmp_path / 'future', TAG, stopped=True)
    (source / 'data.db').write_bytes(b'not sqlite')
    with pytest.raises(sqlite3.DatabaseError):
        create_backup(source, tmp_path / 'corrupt', TAG, stopped=True)


@pytest.mark.parametrize('path', ['', '/absolute', 'C:\\escape', 'skills_pool/../escape', 'skills_pool//empty', 'other.txt', 'skills_pool/.env'])
def test_manifest_file_paths_are_strict(path):
    with pytest.raises(ValueError):
        service.BackupFile(path=path, size=0, sha256='a'*64)


def test_manifest_timestamp_requires_timezone(tmp_path):
    source = tmp_path / 'source'
    database.init_db(source / 'data.db')
    manifest = create_backup(source, tmp_path / 'backup', TAG, stopped=True).model_dump()
    manifest['created_at'] = '2026-10-21T01:00:00'
    with pytest.raises(ValueError, match='时区'):
        service.BackupManifest.model_validate(manifest)


def test_powershell_real_entry_points_and_failure(tmp_path):
    source = tmp_path / 'source'
    database.init_db(source / 'data.db')
    backup, restored = tmp_path / 'backup', tmp_path / 'restored'
    scripts = BACKEND_DIR.parent / 'scripts'
    shell = shutil.which('pwsh') or shutil.which('powershell')
    assert shell, '本平台需要PowerShell脚本实际验收，不能跳过'
    commands = [
        ([shell, '-NoProfile', '-File', str(scripts / 'backup-data.ps1'), '-DataDir', str(source), '-Destination', str(backup), '-CodeTag', TAG], 1),
        ([shell, '-NoProfile', '-File', str(scripts / 'backup-data.ps1'), '-DataDir', str(source), '-Destination', str(backup), '-CodeTag', TAG, '-Stopped'], 0),
        ([shell, '-NoProfile', '-File', str(scripts / 'restore-data.ps1'), '-BackupDir', str(backup), '-Destination', str(restored), '-CodeTag', TAG], 0),
    ]
    for command, code in commands:
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=60)
        assert result.returncode == code, result.stdout + result.stderr
        if code:
            assert '停写' in result.stderr
    assert digest(restored / 'data.db') == digest(source / 'data.db')
