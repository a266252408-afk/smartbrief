"""双状态、隔离旧库备份恢复与真实HTTP契约测试。"""
import hashlib
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from db import database, task_repository as repo
from services.task_outcome import TaskOutcome
from tests.auth_support import ADMIN_TOKEN, READ_TOKEN


def test_contract_rejects_invalid_values():
    for changes in ({"execution_status": "success"}, {"quality_status": "success"}, {"forced_output": "false"}, {"extra": 1}):
        with pytest.raises(ValidationError):
            TaskOutcome(task_id="t", **({"execution_status": "completed", "quality_status": "unknown", "forced_output": False} | changes))


def test_new_task_and_status_roundtrip(tmp_path):
    database.init_db(tmp_path / "new.db")
    repo.create_task("t", "主题")
    assert repo.get_task("t")["quality_status"] == "pending"
    repo.update_task("t", status="completed", quality_status="rejected", forced_output=True, error_code="REVIEW_REJECTED")
    task = repo.get_task("t")
    assert task["execution_status"] == task["status"] == "completed"
    assert task["forced_output"] is True
    assert task["error_code"] == "REVIEW_REJECTED"
    assert repo.list_tasks()[0][0]["quality_status"] == "rejected"
    for fields in ({"quality_status": "bad"}, {"execution_status": "bad"}, {"forced_output": "false"}, {"status": "failed", "execution_status": "completed"}):
        with pytest.raises((ValueError, ValidationError)):
            repo.update_task("t", **fields)
    repo.update_task("t", execution_status="running")
    assert repo.get_task("t")["status"] == "running"
    assert repo.mark_interrupted_tasks_as_paused() == 1
    assert repo.get_task("t")["execution_status"] == "paused"


def test_legacy_backup_restore_and_idempotent_upgrade(tmp_path):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, topic TEXT NOT NULL, status TEXT, final_report TEXT, passed INTEGER, forced_pass INTEGER, review_history TEXT)")
        conn.executemany("INSERT INTO tasks VALUES (?, '旧主题', ?, ?, ?, ?, ?)", [
            ("unknown", "completed", "旧报告", 1, 0, '[{"passed":true}]'),
            ("rejected", "completed", "旧报告", 1, 1, '[{"passed":false,"comments":["证据不足"]}]'),
            ("failed", "failed", None, None, 0, None),
            ("broken", "completed", "旧报告", 1, 0, 'bad json'),
            ("odd", "other", "旧报告", 1, 0, '{}'),
        ])
    # 已关闭写连接，副本哈希/完整性验证后只迁移恢复副本。
    backup = tmp_path / "backup.db"
    shutil.copy2(old, backup)
    assert hashlib.sha256(old.read_bytes()).digest() == hashlib.sha256(backup.read_bytes()).digest()
    restored = tmp_path / "restored.db"
    shutil.copy2(backup, restored)
    with sqlite3.connect(restored) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 5
    database.init_db(restored)
    database.init_db(restored)
    with sqlite3.connect(restored) as conn:
        rows = {row[0]: row[1:] for row in conn.execute("SELECT task_id, execution_status, quality_status, forced_output FROM tasks")}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert rows["unknown"] == ("completed", "unknown", 0)
    assert rows["rejected"] == ("completed", "rejected", 1)
    assert rows["failed"] == ("failed", "not_run", 0)
    assert rows["broken"][1] == "unknown"
    assert rows["odd"][0] == "failed"
    assert old.read_bytes() == backup.read_bytes()


def test_v1_v2_task_report_and_access(tmp_path):
    from main import app
    database.init_db(tmp_path / "api.db")
    repo.create_task("t", "主题")
    repo.update_task("t", status="completed", quality_status="unknown", final_report="报告")
    with TestClient(app, headers={"Authorization": f"Bearer {READ_TOKEN}"}) as client:
        detail = client.get("/api/v2/tasks/t")
        assert detail.status_code == 200
        assert detail.json()["data"]["quality_status"] == "unknown"
        assert detail.json()["meta"]["request_id"]
        report = client.get("/api/v2/tasks/t/report").json()["data"]
        assert report["content_markdown"] == "报告"
        assert report["quality_status"] == "unknown"
        assert client.get("/api/v1/tasks/t/status").json()["status"] == "completed"
        assert client.get("/api/v1/tasks/t/report").json()["quality_status"] == "unknown"
        assert client.get("/api/v2/tasks/missing").json()["error"]["code"] == "TASK_NOT_FOUND"
        assert client.get("/api/v2/tasks/t", headers={"Authorization": ""}).status_code == 401
        assert client.delete("/api/v1/tasks/t").status_code == 403
        repo.create_task("pending", "尚无报告")
        assert client.get("/api/v2/tasks/pending/report").json()["error"]["code"] == "REPORT_NOT_AVAILABLE"
        assert client.get("/api/v2/tasks/missing/report").status_code == 404
        assert client.get("/api/v2/tasks/t", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}).status_code == 200


def test_migration_failure_rolls_back_columns_and_data(tmp_path):
    path = tmp_path / "rollback.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, topic TEXT, status TEXT, final_report TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('t', '主题', 'completed', '原报告')")
        conn.execute("CREATE TRIGGER reject_migration BEFORE UPDATE ON tasks BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        database.init_db(path)
    with sqlite3.connect(path) as conn:
        assert "quality_status" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert conn.execute("SELECT final_report FROM tasks").fetchone()[0] == "原报告"
        conn.execute("DROP TRIGGER reject_migration")
    database.init_db(path)
    assert repo.get_task("t")["quality_status"] == "unknown"
