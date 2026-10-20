"""记忆测试的明确已审核任务夹具；不代替准入服务。"""
from db import task_repository
from db.database import init_db


def approved_report(topic, report, task_id):
    init_db()
    task_repository.create_task(task_id, topic)
    task_repository.update_task(task_id, status="completed", quality_status="passed", final_report=report,
        evidence=[dict(source_id="S1", task_id=task_id, kind="web", title="离线记忆测试来源",
            excerpt="固定测试证据，不是真实市场数据", url="https://example.invalid/memory",
            retrieved_at="2026-10-20T00:00:00Z")])
