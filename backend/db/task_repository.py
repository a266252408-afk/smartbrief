"""任务/ReAct 日志 CRUD（SQLite 持久化）。

结构化字段（plan / completed_steps / research_data / review_comments / review_history
/ memory_hits）以 JSON 文本存取，本层负责序列化/反序列化，业务层无感知。
review_comments / review_history 保存审核意见与历史；
memory_hits 保存长期记忆命中，供前端详情页展示。
"""
import json
from typing import Any

from db.database import get_connection
from services.task_outcome import TaskOutcome
from services.evidence import validate_sources
from services.citations import Citation

_JSON_FIELDS = ("plan", "completed_steps", "research_data", "review_comments",
                "review_history", "memory_hits", "document_ids", "retrievals", "evidence", "citations", "citation_errors")
# 允许更新的字段白名单：防止调用方误传脏字段（如 task_id / created_at）
_ALLOWED_UPDATE = {"status", "topic", "plan", "current_step_index", "completed_steps",
                   "resume_mode",
                   "research_data", "draft_content", "final_report", "error",
                   "review_comments", "review_history", "iteration", "max_iterations",
                   "passed", "forced_pass", "memory_hits", "document_ids", "retrievals", "evidence", "citations", "citation_errors",
                   "execution_status", "quality_status", "forced_output", "error_code"}


def _dumps(value: Any) -> str | None:
    """结构化字段 → JSON 文本（None 直接返回 None）。

    实现说明：SQLite 的 TEXT 列只能存字符串，list/dict 必须
    序列化成 JSON 字符串再落库；读出来时再反序列化。
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)   # ensure_ascii=False 保留中文可读


def _loads(value: str | None, default: Any) -> Any:
    """JSON 文本 → Python 对象（空值/解析失败返回默认值）。

    容错设计：字段为空或损坏时不抛异常，而是返回默认值（如 [] / {}），
    保证业务层拿到的永远是合法结构。
    """
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _row_to_task(row) -> dict | None:
    """sqlite3.Row → dict（并把 JSON 字段反序列化回 Python 对象）。

    转换后业务层拿到的 task 形如：
    {
        "task_id": "task_ab12", "topic": "...", "status": "completed",
        "plan": [...],            # 已是 list（反序列化过）
        "completed_steps": [...], # 已是 list
        "research_data": {...},   # 已是 dict
        "review_comments": [...], # 已是 list（最后一轮审核意见）
        "review_history": [...],  # 已是 list（A2A 各轮审核记录）
        ...
    }
    """
    if row is None:
        return None
    task = dict(row)
    task.pop("run_token", None)  # 内部写入租约不进入v1/v2产品响应。
    task.pop("resume_requested", None)
    task["plan"] = _loads(task.get("plan"), [])                 # JSON -> list
    task["completed_steps"] = _loads(task.get("completed_steps"), [])  # JSON -> list
    task["research_data"] = _loads(task.get("research_data"), {})       # JSON -> dict
    task["review_comments"] = _loads(task.get("review_comments"), [])   # JSON -> list
    task["review_history"] = _loads(task.get("review_history"), [])     # JSON -> list（各轮审核记录）
    task["memory_hits"] = _loads(task.get("memory_hits"), [])           # JSON -> list（长期记忆命中）
    task["citations"] = _loads(task.get("citations"), [])
    task["citation_errors"] = _loads(task.get("citation_errors"), [])
    task["document_ids"] = json.loads(task.get("document_ids") or "[]")
    task["retrievals"] = json.loads(task.get("retrievals") or "[]")
    task["evidence"] = _loads(task.get("evidence"), [])
    if "forced_output" in task:
        task["forced_output"] = bool(task["forced_output"])
    return task


def create_task(task_id: str, topic: str, *, document_ids: list[str] | None = None, regenerated_from: str | None = None, initial_status: str = "running", _connection=None) -> None:
    """插入一条任务初始记录（status 默认 running）。

    由 API 层在提交任务时调用（后台工作流启动之前），
    保证前端立刻能在任务列表里看到这条"执行中"记录。
    """
    from services.retrieval_service import DocumentSelection
    selected = DocumentSelection(document_ids=document_ids or []).document_ids
    if initial_status not in {"queued", "running"}:
        raise ValueError("初始任务必须排队或运行")
    def insert(conn):
        conn.execute(
            "INSERT INTO tasks (task_id, topic, document_ids, regenerated_from,status,execution_status) VALUES (?, ?, ?, ?,?,?)",
            (task_id, topic, _dumps(selected), regenerated_from,initial_status,initial_status),
        )
    if _connection is not None:
        insert(_connection)
    else:
        with get_connection() as conn:
            insert(conn)


def update_task(task_id: str, *, _connection=None, _require_running=False, **fields) -> None:
    """更新任务字段（自动 JSON 序列化，并刷新 updated_at）。

    实现说明：**动态拼 SET 子句** —— 只更新传入的字段；
    结构化字段（plan 等）在写入前自动 _dumps 序列化。
    未在 _ALLOWED_UPDATE 白名单里的字段会被静默忽略，避免脏数据。

    :param task_id: 任务 id
    :param fields: 要更新的字段（keyword 参数）
    """
    updates = {k: v for k, v in fields.items() if k in _ALLOWED_UPDATE}   # 白名单过滤
    if not updates:
        return
    if "status" in updates or "execution_status" in updates:
        status = updates.get("execution_status", updates.get("status"))
        if "status" in updates and updates["status"] != status:
            raise ValueError("执行状态别名不一致")
        updates["status"] = updates["execution_status"] = status
    outcome_fields = {k: v for k, v in updates.items() if k in TaskOutcome.model_fields}
    TaskOutcome(task_id=task_id, **({"execution_status": "running", "quality_status": "pending", "forced_output": False} | outcome_fields))
    from services.retrieval_service import DocumentSelection, RetrievalRecord
    if "document_ids" in updates:
        updates["document_ids"] = DocumentSelection(document_ids=updates["document_ids"]).document_ids
    if "retrievals" in updates:
        records = [RetrievalRecord.model_validate(r) for r in updates["retrievals"]]
        if any(r.task_id != task_id for r in records):
            raise ValueError("检索记录跨任务")
        updates["retrievals"] = [r.model_dump() for r in records]
    if "evidence" in updates:
        updates["evidence"] = validate_sources(task_id, updates["evidence"])
    if "citations" in updates:
        updates["citations"] = [Citation.model_validate(c).model_dump() for c in updates["citations"]]
    sets = []
    params: list = []
    for key, value in updates.items():
        sets.append(f"{key} = ?")
        # 结构化字段走 JSON 序列化，普通字段原样写入
        params.append(_dumps(value) if key in _JSON_FIELDS else value)
    sets.append("updated_at = datetime('now','localtime')")   # 顺便刷新更新时间
    params.append(task_id)
    from services.execution_context import execution_lease, ExecutionStopped
    lease = execution_lease.get()
    guarded = lease is not None and lease[0] == task_id
    where = "task_id = ?"
    if guarded:
        where += " AND run_token = ?"
        params.append(lease[1])
    if guarded or _require_running:
        where += " AND execution_status = 'running'"
    def apply(conn):
        changed = conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE {where}", params).rowcount
        if not changed and (guarded or _require_running):
            raise ExecutionStopped("执行租约已失效，拒绝迟到写入")
    if _connection is not None:
        apply(_connection)
    else:
        with get_connection() as conn:
            apply(conn)


def finalize_task(task_id, fields, logs):
    """最终状态与完整图日志同事务；恢复重读不追加重复历史日志。"""
    from services.execution_context import ExecutionPersistenceError
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            update_task(task_id, _connection=conn, _require_running=True, **fields)
            conn.execute("DELETE FROM task_logs WHERE task_id=?",(task_id,))
            for log in logs:
                conn.execute("INSERT INTO task_logs(task_id,step_id,log_type,content) VALUES (?,?,?,?)",
                    (task_id,log.get("step_id",0),log.get("type","Thought"),str(log.get("content",""))))
    except Exception:
        raise ExecutionPersistenceError("任务结果或日志持久化失败") from None


def get_task(task_id: str) -> dict | None:
    """按 id 查询任务（含反序列化后的结构化字段），不存在返回 None。"""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return _row_to_task(row)


def mark_interrupted_tasks_as_paused(
    message: str = "服务中断：任务已暂停；恢复时将检查快照，必要时从头重试",
) -> int:
    """后端重启后清理：把上次进程遗留的 running 任务统一置为 paused。

    进程被强杀 / 重启时没有任何机会把 running 改成终态（completed / failed），
    这些"僵尸 running"由 lifespan 启动扫描一次性归位为 paused；
    用户随后点恢复；存在待执行节点则续跑，否则明确从头重试。

    :param message: 写入 error 列的中断说明（前端详情可展示）
    :return: 被置为 paused 的任务数
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = 'paused', execution_status = 'paused', error = ?, "
            "updated_at = datetime('now','localtime') WHERE status = 'running'",
            (message,),
        )
    return cur.rowcount


def append_log(task_id: str, step_id: int, log_type: str, content: str) -> None:
    """追加一条 ReAct 日志（task_logs 表）。

    日志类型 log_type 取值为 Thought / Action / Observation，
    前端任务详情页据此区分卡片颜色并分组展示。
    """
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO task_logs (task_id, step_id, log_type, content) VALUES (?, ?, ?, ?)",
            (task_id, step_id, log_type, content),
        )


def get_logs(task_id: str) -> list[dict]:
    """按 task_id 取全部日志（按 id 升序 = 时间顺序，前端日志流顺序一致）。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, task_id, step_id, log_type, content, created_at "
            "FROM task_logs WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_tasks(
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
    keyword: str | None = None,
    order: str = "desc",
) -> tuple[list[dict], int]:
    """分页查询任务列表（任务列表页 / 报告中心数据源）。

    实现说明：返回 `(items, total)` 两个值 —— items 是本页数据，
    total 是**满足过滤条件的总条数**（前端分页组件需要它计算总页数）。
    「报告中心」新增两个查询参数：
    - keyword：按主题模糊匹配（topic LIKE %kw%，SQL 拼接用参数化占位符防注入）
    - order：时间排序（desc 最新在前 / asc 最久在前），默认 desc

    :param status: 可选按状态过滤（running / paused / completed / failed）
    :param limit: 每页条数
    :param offset: 偏移量（第几页 = (page-1) * limit）
    :param keyword: 可选按主题模糊搜索（报告中心搜索框）
    :param order: 可选排序方向：desc（默认，最新在前）/ asc（最久在前）
    :return: (items, total) —— items 为 TaskSummary 字典列表
    """
    conditions: list[str] = []
    params: list = []
    if status:                                    # 状态过滤（可选）
        conditions.append("status = ?")
        params.append(status)
    if keyword:                                   # 主题模糊搜索（报告中心）
        conditions.append("topic LIKE ?")
        params.append(f"%{keyword}%")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    direction = "ASC" if order == "asc" else "DESC"   # 时间排序方向
    with get_connection() as conn:
        # 1) 先数总条数（分页组件需要）
        total = conn.execute(f"SELECT COUNT(*) FROM tasks {where}", params).fetchone()[0]
        # 2) 再查本页数据（按时间排序，LIMIT/OFFSET 分页）
        rows = conn.execute(
            f"SELECT task_id, topic, status, plan, current_step_index, completed_steps, "
            f"resume_mode, execution_status, quality_status, forced_output, error_code, created_at, updated_at FROM tasks {where} "
            f"ORDER BY created_at {direction}, task_id {direction} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    items = [_row_to_task(r) for r in rows]
    return items, total


def delete_task(task_id: str) -> bool:
    """删除任务及其全部 ReAct 日志（tasks + task_logs 联动删除）。

    :param task_id: 任务 id
    :return: True 表示删除了记录；任务不存在返回 False（调用方据此返回 404）
    """
    with get_connection() as conn:
        from db.memory_repository import invalidate_task
        invalidate_task(task_id, conn)
        conn.execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
        cur = conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    return cur.rowcount > 0
