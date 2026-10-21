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
