"""任务队列状态条件更新；不在API或Agent中直接写SQL。"""
from db.database import get_connection


def claim(task_id):
    with get_connection() as conn:
        return conn.execute("UPDATE tasks SET status='running',execution_status='running', "
                            "updated_at=datetime('now','localtime') WHERE task_id=? AND execution_status='queued'",
                            (task_id,)).rowcount == 1


def queued_ids():
    with get_connection() as conn:
        return [r[0] for r in conn.execute("SELECT task_id FROM tasks WHERE execution_status='queued' ORDER BY created_at,rowid")]


def pause(task_id):
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET status='paused',execution_status='paused',error_code='SERVICE_INTERRUPTED', "
                     "error='服务中断，任务已暂停' WHERE task_id=? AND execution_status='running'", (task_id,))
