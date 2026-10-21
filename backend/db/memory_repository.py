"""记忆登记账本；与向量分离，旧向量不会自动取得可信资格。"""
import json
from db.database import get_connection as business_connection
from services.memory_policy import MemoryEntry


def get_connection():
    """合成评测使用独立记忆登记，不读写用户准入记录。"""
    from evaluation.context import evaluation_context
    context = evaluation_context.get()
    return context.memory_connection() if context is not None else business_connection()


def get(memory_id: str) -> MemoryEntry | None:
    with get_connection() as conn:
        row = conn.execute("SELECT data FROM memory_entries WHERE memory_id=?", (memory_id,)).fetchone()
    return MemoryEntry.model_validate(json.loads(row[0])) if row else None


def entries() -> list[MemoryEntry]:
    with get_connection() as conn:
        rows = conn.execute("SELECT data FROM memory_entries ORDER BY memory_id").fetchall()
    return [MemoryEntry.model_validate(json.loads(row[0])) for row in rows]


def reserve(entry: MemoryEntry) -> MemoryEntry:
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO memory_entries(memory_id,data) VALUES(?,?)",
                     (entry.memory_id, entry.model_dump_json()))
    return get(entry.memory_id)


def transition(memory_id: str, status: str, reason: str | None = None) -> bool:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT data FROM memory_entries WHERE memory_id=?", (memory_id,)).fetchone()
        if row is None:
            return False
        data = json.loads(row[0])
        if data["status"] in {"deleted", "invalid"} and status == "active":
            return False
        data.update(status=status, verified=status == "active", invalid_reason=reason)
        entry = MemoryEntry.model_validate(data)
        conn.execute("UPDATE memory_entries SET data=? WHERE memory_id=?", (entry.model_dump_json(), memory_id))
    return True


def invalidate_task(task_id, conn):
    """与原任务删除共用事务；保留审计账本，防止旧向量复活。"""
    rows = conn.execute("SELECT data FROM memory_entries WHERE json_extract(data,'$.task_id')=?", (task_id,)).fetchall()
    for row in rows:
        data = json.loads(row[0])
        if data["status"] == "deleted":
            continue
        data.update(status="invalid", verified=False, invalid_reason="task_deleted")
        entry = MemoryEntry.model_validate(data)
        conn.execute("UPDATE memory_entries SET data=? WHERE memory_id=?", (entry.model_dump_json(),entry.memory_id))
