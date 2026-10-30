"""运行租约的协作检查；取消属于控制流，不能被Agent普通降级吞掉。"""
import asyncio
from contextvars import ContextVar
from contextlib import contextmanager
from services.usage_service import UsagePersistenceError

execution_lease: ContextVar[tuple[str,str] | None] = ContextVar("task_execution_lease",default=None)
model_deadline: ContextVar[float | None] = ContextVar('model_deadline', default=None)


@contextmanager
def deadline_scope(deadline):
    """向模型层传递外层截止时间；嵌套作用域只能缩短期限。"""
    previous = model_deadline.get()
    token = model_deadline.set(min(previous, deadline) if previous is not None else deadline)
    try:
        yield
    finally:
        model_deadline.reset(token)


async def run_before_deadline(awaitable, deadline):
    with deadline_scope(deadline):
        return await awaitable


class ExecutionStopped(asyncio.CancelledError):
    pass


class ExecutionPersistenceError(UsagePersistenceError):
    code = "TASK_PERSISTENCE_FAILED"


def assert_active(task_id=None):
    lease = execution_lease.get()
    if lease is None or task_id is not None and lease[0] != task_id:
        return
    from db.database import get_connection
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT execution_status,run_token FROM tasks WHERE task_id=?",(lease[0],)).fetchone()
    except Exception:
        raise ExecutionPersistenceError("运行状态不可读取，已停止执行") from None
    if row is None or row[0] != "running" or row[1] != lease[1]:
        raise ExecutionStopped("任务已取消、中断或被新执行替代")
