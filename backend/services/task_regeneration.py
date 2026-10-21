"""以新任务保留重生成溯源，不覆盖原报告或修改其质量。"""
from uuid import uuid4
from db import task_repository


class TaskNotTerminal(ValueError):
    pass


def prepare_regeneration(task_id: str) -> dict | None:
    source = task_repository.get_task(task_id)
    if source is None:
        return None
    if source["execution_status"] in {"running", "queued"}:
        raise TaskNotTerminal("运行或排队中的任务不能重新生成")
    new_id = "task_" + uuid4().hex[:8]
    task_repository.create_task(new_id, source["topic"], document_ids=source.get("document_ids", []), regenerated_from=task_id, initial_status="queued")
    return {"task_id":new_id, "topic":source["topic"], "status":"queued", "regenerated_from":task_id}
