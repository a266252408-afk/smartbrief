"""版本化迁移、未来版本拒绝与整事务回滚。"""
import sqlite3

import pytest

from db import database


def test_version_ledger_is_idempotent_and_preserves_report(tmp_path):
    path = tmp_path / 'nested/data.db'
    database.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO tasks(task_id,topic,final_report,quality_status) VALUES('t','主题','原报告','unknown')")
        first = conn.execute('SELECT * FROM schema_migrations').fetchall()
        assert [r[0] for r in first] == [1, 2, 3, 4]
        assert conn.execute('PRAGMA user_version').fetchone()[0] == database.SCHEMA_VERSION
        assert conn.execute('SELECT metadata FROM tasks WHERE task_id=?', ('t',)).fetchone()[0] == '{}'
    database.init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT * FROM schema_migrations').fetchall() == first
        assert conn.execute('SELECT final_report,quality_status,metadata FROM tasks').fetchone() == ('原报告','unknown','{}')


@pytest.mark.parametrize('version', [database.SCHEMA_VERSION + 1, 999])
def test_future_schema_rejected_without_changes(tmp_path, version):
    path = tmp_path / 'future.db'
    with sqlite3.connect(path) as conn:
        conn.execute(f'PRAGMA user_version={version}')
        conn.execute('CREATE TABLE private_data (value TEXT)')
        conn.execute("INSERT INTO private_data VALUES ('保留')")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='版本'):
        database.init_db(path)
    assert path.read_bytes() == before


def test_full_schema_transaction_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / 'failed.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE tasks (task_id TEXT PRIMARY KEY, topic TEXT, status TEXT, final_report TEXT)')
        conn.execute("INSERT INTO tasks VALUES ('old','主题','completed','原报告')")
    original = database.migrate_quality
    def fail(conn):
        original(conn)
        raise RuntimeError('迁移注入故障')
    monkeypatch.setattr(database, 'migrate_quality', fail)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='迁移注入故障'):
        database.init_db(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('tasks',)]


def test_future_ledger_and_sql_failure_are_not_suppressed(tmp_path, monkeypatch):
    path = tmp_path / 'data.db'
    database.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO schema_migrations(version,name) VALUES(9,'未来版本')")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='账本版本'):
        database.init_db(path)
    assert path.read_bytes() == before
    monkeypatch.setattr(database, '_MIGRATIONS', ['ALTER TABLE missing ADD COLUMN x TEXT'])
    with pytest.raises(sqlite3.OperationalError, match='no such table'):
        database.init_db(tmp_path / 'broken.db')
