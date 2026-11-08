"""任务队列状态条件更新；不在API或Agent中直接写SQL。"""
from db.database import get_connection
from uuid import uuid4
import json
from db import task_action_repository as actions


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


def cancel(task_id, key=None):
    from services.task_service import TaskControlError
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cached = actions.replay(conn,key,"cancel",task_id,{})
        if cached is not None:
            return cached
        row = conn.execute("SELECT execution_status FROM tasks WHERE task_id=?",(task_id,)).fetchone()
        if row is None:
            raise TaskControlError("TASK_NOT_FOUND","任务不存在",404)
        if row[0] == "cancelled":
            result = dict(task_id=task_id,status="cancelled",execution_status="cancelled",cancelled=True,already_cancelled=True)
            actions.record(conn,key,"cancel",task_id,{},result)
            return result
        if row[0] not in {"queued","running"}:
            raise TaskControlError("TASK_NOT_CANCELLABLE","任务已终止，不能取消",409)
        conn.execute("UPDATE tasks SET status='cancelled',execution_status='cancelled',quality_status='not_run', "
            "passed=0,forced_output=0,forced_pass=0,final_report=NULL,error_code='TASK_CANCELLED',error='用户已取消任务', "
            "updated_at=datetime('now','localtime') WHERE task_id=?",(task_id,))
        conn.execute("INSERT INTO task_logs(task_id,step_id,log_type,content) VALUES (?,0,'Observation','用户已取消任务')",(task_id,))
        result = dict(task_id=task_id,status="cancelled",execution_status="cancelled",cancelled=True,already_cancelled=False)
        actions.record(conn,key,"cancel",task_id,{},result)
        return result


def create_job(topic=None, document_ids=None, *, key=None, source_id=None, metadata=None):
    from db import task_repository
    from app_version import APP_VERSION
    from services.task_regeneration import TaskNotTerminal
    action = "regenerate" if source_id is not None else "create"
    body = {} if source_id is not None else {"topic":topic,"document_ids":document_ids or []}
    target = source_id or ""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cached = actions.replay(conn,key,action,target,body)
        if cached is not None:
            return cached
        if source_id is not None:
            row = conn.execute("SELECT topic,document_ids,execution_status FROM tasks WHERE task_id=?",(source_id,)).fetchone()
            if row is None:
                return None
            if row["execution_status"] in {"queued","running"}:
                raise TaskNotTerminal("运行或排队中的任务不能重新生成")
            topic, document_ids = row["topic"], json.loads(row["document_ids"])
        task_id = "task_" + uuid4().hex
        task_repository.create_task(task_id,topic,document_ids=document_ids,regenerated_from=source_id,
                                     initial_status="queued",metadata=metadata if metadata is not None else
                                     dict(generator_version=APP_VERSION, execution_mode='online', report_pipeline_version=2),_connection=conn)
        result = dict(task_id=task_id,topic=topic,status="queued")
        if source_id is None:
            result["execution_status"] = "queued"
        else:
            result["regenerated_from"] = source_id
        actions.record(conn,key,action,target,body,result)
        return result


def requested_resume(task_id):
    with get_connection() as conn:
        row = conn.execute("SELECT resume_requested FROM tasks WHERE task_id=?",(task_id,)).fetchone()
    return bool(row and row[0])


def resume(task_id, mode, key=None):
    from services.task_service import TaskControlError
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cached = actions.replay(conn,key,"resume",task_id,{})
        if cached is not None:
            return cached, False
        row = conn.execute("SELECT execution_status,resume_mode FROM tasks WHERE task_id=?",(task_id,)).fetchone()
        if row is None:
            raise TaskControlError("TASK_NOT_FOUND","任务不存在",404)
        state = row["execution_status"]
        if state in {"completed","cancelled"}:
            message = "任务已完成，无需恢复" if state == "completed" else "已取消任务不可恢复"
            raise TaskControlError("TASK_NOT_RESUMABLE",message,409)
        fresh = state in {"paused","failed"}
        if fresh:
            conn.execute("UPDATE tasks SET status='queued',execution_status='queued',run_token=NULL,resume_requested=1, "
                "resume_mode=?,quality_status='pending',passed=NULL,forced_pass=0,forced_output=0, "
                "final_report=NULL,error=NULL,error_code=NULL,updated_at=datetime('now','localtime') WHERE task_id=?",(mode,task_id))
            state = "queued"
        else:
            mode = row["resume_mode"]
        result = dict(task_id=task_id,status=state,execution_status=state,resumed=fresh,resume_mode=mode)
        actions.record(conn,key,"resume",task_id,{},result)
        return result,fresh


def reset_for_restart(task_id, initial):
    from db import task_repository
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        fields = {k:v for k,v in initial.items() if k in task_repository._ALLOWED_UPDATE}
        fields["resume_mode"] = "restart"
        task_repository.update_task(task_id,_connection=conn,_require_running=True,**fields)
        conn.execute("DELETE FROM task_logs WHERE task_id=?",(task_id,))
