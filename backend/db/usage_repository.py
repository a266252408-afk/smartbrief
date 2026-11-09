"""模型调用持久化边界。"""
from db.database import get_connection


def _decode(row):
    from services.usage_service import ModelUsage
    entry = ModelUsage.model_validate_json(row["data"])
    if entry.task_id != row["task_id"] or entry.call_id != row["call_id"]:
        raise ValueError("用量记录归属损坏")
    return entry.model_dump()


def save(entry, *, _connection=None):
    def insert(conn):
        conn.execute("INSERT INTO model_usage(call_id,task_id,data) VALUES (?,?,?) "
                     "ON CONFLICT(call_id) DO UPDATE SET data=excluded.data WHERE model_usage.task_id=excluded.task_id",
                     (entry.call_id, entry.task_id, entry.model_dump_json()))
    if _connection is not None:
        insert(_connection)
    else:
        with get_connection() as conn:
            insert(conn)


def list_usage(task_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT call_id,task_id,data FROM model_usage WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
    return [_decode(row) for row in rows]


def reserve(entry, limit, *, max_calls=100, max_seconds=3600):
    from services.budget import charged
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute("SELECT execution_status FROM tasks WHERE task_id=?", (entry.task_id,)).fetchone()
        if task and task[0] != "running":
            return "stopped"
        rows = conn.execute("SELECT call_id,task_id,data FROM model_usage WHERE task_id=?", (entry.task_id,)).fetchall()
        decoded = [_decode(r) for r in rows]
        used = sum(charged(r, limit) for r in decoded)
        calls = sum(r['status'] != 'budget_exceeded' for r in decoded)
        seconds = sum((r.get('reserved_seconds') or max_seconds) if r['status']=='running' or r.get('error_code')=='USAGE_INTERRUPTED'
                      else r['duration_ms']/1000 for r in decoded if r['status']!='budget_exceeded')
        remaining = max_seconds - seconds
        status = "reserved"
        if used + entry.reserved_tokens > limit or calls >= max_calls or remaining <= 0:
            entry.status, entry.error_code, entry.reserved_tokens = "budget_exceeded", "BUDGET_EXCEEDED", 0
            entry.reserved_seconds = 0
            status = "exceeded"
        else:
            entry.reserved_seconds = min(entry.reserved_seconds or max_seconds, remaining)
        conn.execute("INSERT INTO model_usage(call_id,task_id,data) VALUES (?,?,?)",
                     (entry.call_id, entry.task_id, entry.model_dump_json()))
        return status
