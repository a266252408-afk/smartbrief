"""任务来源读取服务，HTTP层不直接组装仓储数据。"""
from db import task_repository
from services.evidence import validate_sources


def get_sources(task_id: str) -> dict | None:
    task = task_repository.get_task(task_id)
    if task is None:
        return None
    return {"task_id": task_id, "sources": validate_sources(task_id, task.get("evidence", []))}
