"""任务队列状态条件更新；不在API或Agent中直接写SQL。"""
from db.database import get_connection
from uuid import uuid4


def claim(task_id):
    token = uuid4().hex
    with get_connection() as conn:
        changed = conn.execute("UPDATE tasks SET status='running',execution_status='running',run_token=?, "
                            "updated_at=datetime('now','localtime') WHERE task_id=? AND execution_status='queued'",
                            (token,task_id)).rowcount == 1
    return token if changed else None


def queued_ids():
    with get_connection() as conn:
        return [r[0] for r in conn.execute("SELECT task_id FROM tasks WHERE execution_status='queued' ORDER BY created_at,rowid")]


def pause(task_id, token=None):
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET status='paused',execution_status='paused',error_code='SERVICE_INTERRUPTED', "
                     "error='服务中断，任务已暂停' WHERE task_id=? AND execution_status='running' "
                     "AND run_token=?", (task_id,token))


def cancel(task_id):
    from services.task_service import TaskControlError
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT execution_status FROM tasks WHERE task_id=?",(task_id,)).fetchone()
        if row is None:
            raise TaskControlError("TASK_NOT_FOUND","任务不存在",404)
        if row[0] == "cancelled":
            return True
        if row[0] not in {"queued","running"}:
            raise TaskControlError("TASK_NOT_CANCELLABLE","任务已终止，不能取消",409)
        conn.execute("UPDATE tasks SET status='cancelled',execution_status='cancelled',quality_status='not_run', "
            "passed=0,forced_output=0,forced_pass=0,final_report=NULL,error_code='TASK_CANCELLED',error='用户已取消任务', "
            "updated_at=datetime('now','localtime') WHERE task_id=?",(task_id,))
        conn.execute("INSERT INTO task_logs(task_id,step_id,log_type,content) VALUES (?,0,'Observation','用户已取消任务')",(task_id,))
        return False
