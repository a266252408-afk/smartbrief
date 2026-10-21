"""模型调用持久化边界。"""
from db.database import get_connection


def _decode(row):
    from services.usage_service import ModelUsage
    entry = ModelUsage.model_validate_json(row["data"])
    if entry.task_id != row["task_id"] or entry.call_id != row["call_id"]:
        raise ValueError("用量记录归属损坏")
    return entry.model_dump()


def save(entry):
    with get_connection() as conn:
        conn.execute("INSERT INTO model_usage(call_id,task_id,data) VALUES (?,?,?) "
                     "ON CONFLICT(call_id) DO UPDATE SET data=excluded.data WHERE model_usage.task_id=excluded.task_id",
                     (entry.call_id, entry.task_id, entry.model_dump_json()))


def list_usage(task_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT call_id,task_id,data FROM model_usage WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
    return [_decode(row) for row in rows]


def reserve(entry, limit):
    from services.budget import charged
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute("SELECT execution_status FROM tasks WHERE task_id=?", (entry.task_id,)).fetchone()
        if task and task[0] != "running":
            return "stopped"
        rows = conn.execute("SELECT call_id,task_id,data FROM model_usage WHERE task_id=?", (entry.task_id,)).fetchall()
        used = sum(charged(_decode(r), limit) for r in rows)
        status = "reserved"
        if used + entry.reserved_tokens > limit:
            entry.status, entry.error_code, entry.reserved_tokens = "budget_exceeded", "BUDGET_EXCEEDED", 0
            status = "exceeded"
        conn.execute("INSERT INTO model_usage(call_id,task_id,data) VALUES (?,?,?)",
                     (entry.call_id, entry.task_id, entry.model_dump_json()))
        return status
