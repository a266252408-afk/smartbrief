"""评测报告 CRUD（SQLite 持久化）。

评测记录存 `data.db` 的 `evaluation_reports` 表（与 tasks / task_logs 同库），
结构字段（results / metrics）以 JSON 文本存取，序列化/反序列化封装在本层。

生命周期：
- create()          → 评测开始（status=running，前端可立即轮询到进度）
- set_progress()    → 每跑完一个用例更新 completed_cases
- finish()          → 全部用例完成，写入结果/指标/Markdown（status=completed）
- fail()            → 任一异常导致评测中断（status=failed）
"""
import json
from typing import Any

from db.database import get_connection

_JSON_FIELDS = ("results", "metrics")
# 允许更新的字段白名单：防止调用方误传脏字段（如 eval_id / created_at）
_ALLOWED_UPDATE = {"status", "completed_cases", "results", "metrics", "report_md", "error"}


def _dumps(value: Any) -> str | None:
    """结构化字段 → JSON 文本（None 直接返回 None）。"""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any) -> Any:
    """JSON 文本 → Python 对象（空值/解析失败返回默认值，保证业务层拿到合法结构）。"""
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _row_to_eval(row) -> dict | None:
    """sqlite3.Row → dict（并把 JSON 字段反序列化回 Python 对象）。"""
    if row is None:
        return None
    record = dict(row)
    record["results"] = _loads(record.get("results"), [])   # JSON -> list（用例明细）
    record["metrics"] = _loads(record.get("metrics"), {})   # JSON -> dict（指标汇总）
    return record


def create(eval_id: str, total_cases: int) -> None:
    """插入一条评测初始记录（status 默认 running）。

    由评测 runner 在开始前调用，保证前端提交后立刻能查到
    该评测并轮询进度（completed_cases / total_cases）。
    """
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO evaluation_reports (eval_id, total_cases) VALUES (?, ?)",
            (eval_id, total_cases),
        )


def _update(eval_id: str, **fields) -> None:
    """更新评测字段（结构化字段自动 JSON 序列化，并刷新 updated_at）。"""
    updates = {k: v for k, v in fields.items() if k in _ALLOWED_UPDATE}
    if not updates:
        return
    sets = []
    params: list = []
    for key, value in updates.items():
        sets.append(f"{key} = ?")
        params.append(_dumps(value) if key in _JSON_FIELDS else value)
    sets.append("updated_at = datetime('now','localtime')")
    params.append(eval_id)
    with get_connection() as conn:
        conn.execute(f"UPDATE evaluation_reports SET {', '.join(sets)} WHERE eval_id = ?", params)


def set_progress(eval_id: str, completed_cases: int) -> None:
    """更新已完成用例数（评测执行中前端进度条轮询的数据源）。"""
    _update(eval_id, completed_cases=completed_cases)


def finish(eval_id: str, results: list[dict], metrics: dict, report_md: str) -> None:
    """评测全部完成：一次性写入用例明细、指标汇总与 Markdown 报告（status=completed）。"""
    _update(
        eval_id,
        status="completed",
        completed_cases=len(results),
        results=results,
        metrics=metrics,
        report_md=report_md,
    )


def fail(eval_id: str, error: str) -> None:
    """评测中断（异常兜底）：标记 failed 并记录原因，前端可提示重试。"""
    _update(eval_id, status="failed", error=error)


def fail_running_interrupted(message: str = "服务中断：评测未完成，已标记失败，可重新触发") -> int:
    """后端启动清理：把上次进程遗留的 running 评测统一置为 failed。

    与任务侧的 `mark_interrupted_tasks_as_paused` 同理 —— 评测在后台
    asyncio 任务里执行，进程被强杀/重启时没有机会写入终态，遗留的
    running 记录会让前端评测中心进度条永久卡住且「运行评测」被禁用。
    lifespan 启动扫描一次性归位，用户即可重新触发评测。

    :param message: 写入 error 列的中断说明（前端评测中心失败提示）
    :return: 被置为 failed 的评测数
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE evaluation_reports SET status = 'failed', error = ?, "
            "updated_at = datetime('now','localtime') WHERE status = 'running'",
            (message,),
        )
    return cur.rowcount


def get(eval_id: str) -> dict | None:
    """按 id 查询评测报告（含反序列化后的 results / metrics），不存在返回 None。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM evaluation_reports WHERE eval_id = ?", (eval_id,)
        ).fetchone()
    return _row_to_eval(row)


def delete(eval_id: str) -> bool:
    """删除评测报告（评测无子表，无需级联）。

    :param eval_id: 评测 id
    :return: True 表示删除了记录；不存在返回 False（调用方据此返回 404）
    """
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM evaluation_reports WHERE eval_id = ?", (eval_id,))
    return cur.rowcount > 0


def list_reports(limit: int = 10, offset: int = 0) -> tuple[list[dict], int]:
    """分页查询评测报告列表（评测中心历史列表数据源，最新在前）。

    实现说明：返回 `(items, total)` —— items 是本页数据，total 是总条数
    （前端分页组件计算总页数用）。列表项不返回大文本（results/report_md），
    详情由 `get()` 单独查询，避免列表接口载荷过大。
    """
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM evaluation_reports").fetchone()[0]
        rows = conn.execute(
            "SELECT eval_id, status, total_cases, completed_cases, error, "
            "created_at, updated_at FROM evaluation_reports "
            "ORDER BY created_at DESC, eval_id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    items = [dict(r) for r in rows]
    return items, total