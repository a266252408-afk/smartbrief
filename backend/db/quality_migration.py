"""质量列迁移：仅回填新增列，事务失败向调用方传播。"""
import json

from services.task_outcome import ExecutionStatus
from typing import get_args


def migrate_quality(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    additions = {
        "execution_status": "TEXT NOT NULL DEFAULT 'running'",
        "quality_status": "TEXT NOT NULL DEFAULT 'pending'",
        "forced_output": "INTEGER NOT NULL DEFAULT 0",
        "error_code": "TEXT",
    }
    if set(additions) <= columns:
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            conn.execute("PRAGMA user_version=1")
        return
    for name, sql_type in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {sql_type}")
    for row in conn.execute("SELECT task_id,status,final_report,passed,forced_pass,review_history FROM tasks").fetchall():
        task_id, status, report, passed, forced, history = row
        execution = status if status in get_args(ExecutionStatus) else "failed"
        quality = "unknown"
        if execution == "failed" and not report:
            quality = "not_run"
        try:
            records = json.loads(history or "[]")
        except (ValueError, TypeError):
            records = []
        # 旧passed=True可能由失败放行产生，不能凭旧布尔值升级为passed。
        if forced and isinstance(records, list) and any(
            isinstance(r, dict) and r.get("passed") is False for r in records
        ):
            quality = "rejected"
        conn.execute(
            "UPDATE tasks SET execution_status=?,quality_status=?,forced_output=? WHERE task_id=?",
            (execution, quality, bool(forced), task_id),
        )
    conn.execute("PRAGMA user_version=1")
