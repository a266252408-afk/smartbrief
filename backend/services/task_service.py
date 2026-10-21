"""单worker任务队列服务；生命周期与句柄归属集中管理。"""
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
import asyncio
import logging
import os
from uuid import uuid4

from db import database, task_repository, task_control_repository as controls
from services.retrieval_service import DocumentSelection

logger = logging.getLogger(__name__)


class TaskControlError(RuntimeError):
    def __init__(self, code, message, status=503):
        super().__init__(message)
        self.code, self.status = code, status


def concurrency_limit():
    try:
        limit = int(os.getenv("MAX_CONCURRENT_TASKS", "2"))
        if limit < 1:
            raise ValueError
        for key in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
            if int(os.getenv(key, "1")) != 1:
                raise ValueError
        return limit
    except ValueError:
        raise TaskControlError("TASK_CONFIG_INVALID", "任务并发配置无效，必须单worker且并发数为正整数") from None


class TaskAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    topic: str
    status: Literal["queued"] = "queued"
    execution_status: Literal["queued"] = "queued"


class TaskCreateRequest(DocumentSelection):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    topic: str = Field(min_length=1, max_length=200)


class TaskCancelled(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    status: Literal["cancelled"] = "cancelled"
    execution_status: Literal["cancelled"] = "cancelled"
    cancelled: Literal[True] = True
    already_cancelled: bool


class TaskService:
    def __init__(self, *, max_concurrency=2, runner=None):
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("并发数必须为正整数")
        self.max_concurrency = max_concurrency
        self.runner = runner
        self.path = database.DB_PATH
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.handles = {}
        self.closing = False

    def start(self):
        for task_id in controls.queued_ids():
            self.schedule(task_id)

    async def submit(self, topic, document_ids=None):
        if self.closing:
            raise TaskControlError("TASK_SERVICE_STOPPED", "任务服务正在关闭")
        task_id = "task_" + uuid4().hex
        try:
            body = TaskCreateRequest(topic=topic, document_ids=document_ids or [])
        except ValueError:
            raise TaskControlError("INVALID_TASK_REQUEST", "主题或文档选择无效", 422) from None
        try:
            task_repository.create_task(task_id, body.topic, document_ids=body.document_ids, initial_status="queued")
        except Exception:
            raise TaskControlError("TASK_CREATE_FAILED", "任务创建失败") from None
        self.schedule(task_id)
        return TaskAccepted(task_id=task_id, topic=body.topic).model_dump()

    def schedule(self, task_id, *, resume=False):
        if self.closing:
            raise TaskControlError("TASK_SERVICE_STOPPED", "任务服务正在关闭")
        handle = self.handles.get(task_id)
        if handle is None or handle.done():
            handle = asyncio.create_task(self._execute(task_id, resume))
            self.handles[task_id] = handle
            handle.add_done_callback(lambda done: self.handles.pop(task_id, None) if self.handles.get(task_id) is done else None)
        return handle

    async def _execute(self, task_id, resume):
        from services.execution_context import execution_lease
        context = None
        try:
            async with self.semaphore:
                token = controls.claim(task_id)
                if not token:
                    return
                context = execution_lease.set((task_id,token))
                task = task_repository.get_task(task_id)
                if self.runner is None:
                    from graph.workflow import run_task
                    await run_task(task_id, task["topic"], resume=resume)
                else:
                    await self.runner(task_id, task["topic"], resume=resume)
                current = task_repository.get_task(task_id)
                if current and current["execution_status"] == "running":
                    task_repository.update_task(task_id, status="failed", quality_status="not_run",
                        error_code="WORKFLOW_RESULT_MISSING", error="工作流未持久化终态")
        except asyncio.CancelledError:
            try:
                if context is not None:
                    controls.pause(task_id, token)
            except Exception:
                logger.warning("任务 %s 暂停状态不可用，留待启动时复核", task_id)
        except Exception:
            logger.warning("任务执行或持久化不可用，任务 %s 保留实际落库状态", task_id)
            try:
                current = task_repository.get_task(task_id)
                if current and current["execution_status"] == "running":
                    task_repository.update_task(task_id, status="failed", quality_status="not_run",
                        error_code="TASK_EXECUTION_FAILED", error="任务执行失败")
            except Exception:
                logger.warning("任务 %s 状态写入不可用，不能标记成功", task_id)
        finally:
            if context is not None:
                execution_lease.reset(context)

    async def cancel(self, task_id):
        try:
            already = controls.cancel(task_id)
        except TaskControlError:
            raise
        except Exception:
            raise TaskControlError("TASK_CANCEL_FAILED","取消状态持久化失败") from None
        handle = self.handles.get(task_id)
        if handle is not None:
            handle.cancel()
        return TaskCancelled(task_id=task_id,already_cancelled=already).model_dump()

    async def wait(self, task_id):
        handle = self.handles.get(task_id)
        if handle is not None:
            await asyncio.shield(handle)
        task = task_repository.get_task(task_id)
        if task is None:
            raise TaskControlError("TASK_NOT_FOUND", "任务不存在", 404)
        return task

    async def close(self):
        self.closing = True
        handles = list(self.handles.values())
        for handle in handles:
            handle.cancel()
        await asyncio.gather(*handles, return_exceptions=True)
        self.handles.clear()


def get_task_service():
    loop = asyncio.get_running_loop()
    service = getattr(loop, "_smartbrief_task_service", None)
    if service is None or service.closing or service.path != database.DB_PATH:
        service = TaskService(max_concurrency=concurrency_limit())
        loop._smartbrief_task_service = service
    return service
