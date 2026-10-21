"""停服务备份与副本恢复；清单与原始文件不被校验过程修改。"""
from datetime import datetime, timezone, timedelta
from contextlib import closing
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from db.database import SCHEMA_VERSION
from services.file_security import validate_component
from services.instance_lock import InstanceLock

_FILES = {'data.db', 'checkpoints.db', 'tool_config.json', 'mcp_config.json', 'skill_config.json'}
_FILES |= {f'{db}{suffix}' for db in ('data.db', 'checkpoints.db') for suffix in ('-wal', '-shm')}
_DIRECTORIES = ('data/chroma_db', 'data/uploads', 'skills_pool')


def _allowed(name: str) -> bool:
    return name in _FILES or any(name.startswith(prefix + '/') for prefix in _DIRECTORIES)


def _excluded(name: str) -> bool:
    return any(part.startswith('.env') or part == '__pycache__' or part.endswith('.pyc') for part in PurePosixPath(name).parts)


class BackupFile(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')

    @field_validator('path')
    @classmethod
    def safe_path(cls, value):
        if not value or value.startswith('/') or '\\' in value:
            raise ValueError('备份路径非法')
        for part in value.split('/'):
            validate_component(part)
        if not _allowed(value) or _excluded(value):
            raise ValueError('备份路径不在允许清单或包含密钥文件')
        return value


class BackupManifest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal[1] = 1
    data_schema_version: int = Field(ge=0)
    code_tag: str = Field(min_length=1)
    created_at: str
    timezone: Literal['Asia/Shanghai'] = 'Asia/Shanghai'
    consistency: Literal['stopped_with_instance_lock'] = 'stopped_with_instance_lock'
    files: list[BackupFile] = Field(min_length=1)
    checks: dict

    @field_validator('schema_version', mode='before')
    @classmethod
    def integer_format_version(cls, value):
        if type(value) is not int:
            raise ValueError('清单版本必须为整数')
        return value

    @model_validator(mode='after')
    def unique_files(self):
        names = [item.path.casefold() for item in self.files]
        if len(names) != len(set(names)) or 'data.db' not in names:
            raise ValueError('清单文件重复或缺业务库')
        if datetime.fromisoformat(self.created_at).utcoffset() is None:
            raise ValueError('备份时间必须包含时区')
        return self


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _absolute(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('必须明确指定绝对路径')
    return path.resolve()


def _new_destination(source: Path, destination: Path) -> Path:
    target = _absolute(destination)
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError('目标目录不能与源目录重叠')
    if target.exists():
        raise FileExistsError('目标必须为全新目录，禁止覆盖')
    return target


def _inventory(source: Path) -> list[BackupFile]:
    paths = [source / name for name in sorted(_FILES)]
    for prefix in _DIRECTORIES:
        directory = source / prefix
        if directory.is_symlink():
            raise ValueError('数据目录禁止链接')
        if directory.exists():
            paths.extend(directory.rglob('*'))
    entries = []
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError('备份文件禁止链接或路径越界')
        relative = path.relative_to(source).as_posix()
        if path.is_file() and not _excluded(relative):
            entries.append(BackupFile(path=relative, size=path.stat().st_size, sha256=_digest(path)))
    if not any(item.path == 'data.db' for item in entries):
        raise ValueError('源目录缺少业务库data.db')
    return entries


def _copy(source: Path, destination: Path, files: list[BackupFile]):
    for entry in files:
        target = destination / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / entry.path, target)


def _check_files(root: Path, files: list[BackupFile], *, manifest: bool = False):
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('备份存在链接或路径越界')
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    expected = {entry.path for entry in files} | ({'manifest.json'} if manifest else set())
    if actual != expected:
        raise ValueError('备份文件清单不一致：存在额外或缺失文件')
    for entry in files:
        path = root / entry.path
        if path.stat().st_size != entry.size or _digest(path) != entry.sha256:
            raise ValueError('备份文件大小或哈希不一致')


def _inspect(root: Path, files: list[BackupFile]) -> dict:
    # SQLite/Chroma打开时可能修改SHM或元数据，只在临时副本检查。
    with TemporaryDirectory(prefix='smartbrief-verify-') as temporary:
        clone = Path(temporary)
        _copy(root, clone, files)
        sqlite_checks = {}
        for name in ('data.db', 'checkpoints.db', 'data/chroma_db/chroma.sqlite3'):
            path = clone / name
            if path.is_file():
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
                    if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                        raise ValueError('SQLite完整性校验失败')
                    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                    counts = {name: conn.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}
                    sqlite_checks[path.relative_to(clone).as_posix()] = {'user_version': conn.execute('PRAGMA user_version').fetchone()[0], 'rows': counts}
        if sqlite_checks['data.db']['user_version'] > SCHEMA_VERSION:
            raise ValueError('备份数据版本高于当前代码')
        for name in ('tool_config.json', 'mcp_config.json', 'skill_config.json'):
            path = clone / name
            if path.exists() and not isinstance(json.loads(path.read_text('utf-8')), dict):
                raise ValueError('运行配置必须为JSON对象')
        collections = []
        if (clone / 'data/chroma_db').exists():
            import chromadb
            from chromadb.config import Settings
            client = chromadb.PersistentClient(path=str(clone / 'data/chroma_db'), settings=Settings(anonymized_telemetry=False))
            try:
                for item in client.list_collections():
                    collection = client.get_collection(item.name, embedding_function=None)
                    count = collection.count()
                    if count:
                        sample = collection.get(limit=1, include=['embeddings'])
                        result = collection.query(query_embeddings=[sample['embeddings'][0].tolist()], n_results=1, include=['distances'])
                        if not result['ids'][0] or abs(result['distances'][0][0]) > 0.001:
                            raise ValueError('向量恢复检索校验失败')
                    collections.append({'name': item.name, 'count': count})
            finally:
                client._system.stop()
        return {'sqlite': sqlite_checks, 'collections': sorted(collections, key=lambda item: item['name'])}


def create_backup(source: Path, destination: Path, code_tag: str, stopped: bool = False) -> BackupManifest:
    if not stopped:
        raise ValueError('必须先停止服务，并显式确认停写')
    if not code_tag.strip():
        raise ValueError('必须记录代码标签')
    source = _absolute(source)
    if not source.is_dir():
        raise ValueError('源数据目录不存在')
    target = _new_destination(source, destination)
    lock = InstanceLock(source / 'data.runner.lock')
    lock.acquire()
    try:
        files = _inventory(source)
        target.mkdir(parents=True, exist_ok=False)
        _copy(source, target, files)
        _check_files(target, files)
        checks = _inspect(target, files)
        if _inventory(source) != files:
            raise ValueError('备份期间源数据发生变化，禁止生成成功清单')
        result = BackupManifest(data_schema_version=checks['sqlite']['data.db']['user_version'], code_tag=code_tag, created_at=datetime.now(timezone(timedelta(hours=8))).isoformat(), files=files, checks=checks)
        with (target / 'manifest.json').open('x', encoding='utf-8') as stream:
            stream.write(result.model_dump_json(indent=2) + '\n')
        return result
    finally:
        lock.release()


def restore_backup(backup: Path, destination: Path, code_tag: str) -> BackupManifest:
    backup = _absolute(backup)
    target = _new_destination(backup, destination)
    manifest = BackupManifest.model_validate_json((backup / 'manifest.json').read_text('utf-8'))
    if manifest.code_tag != code_tag:
        raise ValueError('代码标签不匹配，禁止直接切换数据')
    if manifest.data_schema_version > SCHEMA_VERSION:
        raise ValueError('数据版本高于当前代码')
    _check_files(backup, manifest.files, manifest=True)
    actual = _inspect(backup, manifest.files)
    if actual != manifest.checks or actual['sqlite']['data.db']['user_version'] != manifest.data_schema_version:
        raise ValueError('备份业务行数或检索校验与清单不一致')
    target.mkdir(parents=True, exist_ok=False)
    _copy(backup, target, manifest.files)
    _check_files(target, manifest.files)
    if _inspect(target, manifest.files) != manifest.checks:
        raise ValueError('恢复副本业务校验失败')
    return manifest
