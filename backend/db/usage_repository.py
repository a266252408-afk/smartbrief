"""模型调用持久化边界。"""
from db.database import get_connection


def save(entry):
    with get_connection() as conn:
        conn.execute("INSERT INTO model_usage(call_id,task_id,data) VALUES (?,?,?) "
                     "ON CONFLICT(call_id) DO UPDATE SET data=excluded.data WHERE model_usage.task_id=excluded.task_id",
                     (entry.call_id, entry.task_id, entry.model_dump_json()))


def list_usage(task_id: str) -> list[dict]:
    from services.usage_service import ModelUsage
    with get_connection() as conn:
        rows = conn.execute("SELECT data FROM model_usage WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
    return [ModelUsage.model_validate_json(row[0]).model_dump() for row in rows]


def reserve(entry, limit):
    from services.budget import charged
    from services.usage_service import ModelUsage
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute("SELECT execution_status FROM tasks WHERE task_id=?", (entry.task_id,)).fetchone()
        if task and task[0] != "running":
            return "stopped"
        rows = conn.execute("SELECT data FROM model_usage WHERE task_id=?", (entry.task_id,)).fetchall()
        used = sum(charged(ModelUsage.model_validate_json(r[0]).model_dump(), limit) for r in rows)
        status = "reserved"
        if used + entry.reserved_tokens > limit:
            entry.status, entry.error_code, entry.reserved_tokens = "budget_exceeded", "BUDGET_EXCEEDED", 0
            status = "exceeded"
        conn.execute("INSERT INTO model_usage(call_id,task_id,data) VALUES (?,?,?)",
                     (entry.call_id, entry.task_id, entry.model_dump_json()))
        return status
