"""单worker任务队列服务；生命周期与句柄归属集中管理。"""
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal
import asyncio
import logging
import os
import hashlib
import re

from db import database, task_repository, task_control_repository as controls
from services.retrieval_service import DocumentSelection

logger = logging.getLogger(__name__)


def key_digest(key):
    if key is None:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}",key):
        raise TaskControlError("INVALID_IDEMPOTENCY_KEY","幂等键须为1至128位字母、数字或._:-",422)
    return hashlib.sha256(key.encode()).hexdigest()


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


class TaskResumed(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True)
    task_id: str
    status: Literal["queued","running"]
    execution_status: Literal["queued","running"]
    resumed: bool
    resume_mode: Literal["checkpoint","restart"] | None

    @model_validator(mode="after")
    def consistent(self):
        if self.status != self.execution_status or self.resumed and (self.status != "queued" or self.resume_mode is None):
            raise ValueError("恢复接收状态不一致")
        return self


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

    async def submit(self, topic, document_ids=None, *, key=None):
        if self.closing:
            raise TaskControlError("TASK_SERVICE_STOPPED", "任务服务正在关闭")
        digest = key_digest(key)
        try:
            body = TaskCreateRequest(topic=topic, document_ids=document_ids or [])
        except ValueError:
            raise TaskControlError("INVALID_TASK_REQUEST", "主题或文档选择无效", 422) from None
        try:
            result = controls.create_job(body.topic,body.document_ids,key=digest)
            result = TaskAccepted.model_validate(result).model_dump()
        except TaskControlError:
            raise
        except Exception:
            raise TaskControlError("TASK_CREATE_FAILED", "任务创建失败") from None
        self.schedule(result["task_id"])
        return result

    def schedule(self, task_id, *, resume=False, replace=False):
        if self.closing:
            raise TaskControlError("TASK_SERVICE_STOPPED", "任务服务正在关闭")
        handle = self.handles.get(task_id)
        if replace and handle is not None:
            handle.cancel()
        if replace or handle is None or handle.done():
            handle = asyncio.create_task(self._execute(task_id, resume))
            self.handles[task_id] = handle
            handle.add_done_callback(lambda done: self.handles.pop(task_id, None) if self.handles.get(task_id) is done else None)
        return handle

    async def _execute(self, task_id, resume):
        from services.execution_context import execution_lease, ExecutionStopped
        context = None
        try:
            async with self.semaphore:
                token = controls.claim(task_id)
                if not token:
                    return
                context = execution_lease.set((task_id,token))
                task = task_repository.get_task(task_id)
                resume = resume or controls.requested_resume(task_id)
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
            if context is not None:
                try:
                    # 只使用已领取租约条件写终态，不依赖可能损坏的任务JSON重读。
                    task_repository.update_task(task_id, _require_running=True, status="failed", quality_status="not_run",
                        forced_output=False, forced_pass=False, passed=False,
                        error_code="TASK_EXECUTION_FAILED", error="任务执行失败")
                except ExecutionStopped:
                    pass  # 取消/完成/新租约已先赢得状态竞争，不覆盖。
                except Exception:
                    logger.warning("任务 %s 状态写入不可用，不能标记成功", task_id)
        finally:
            if context is not None:
                execution_lease.reset(context)

    def _reject_demo(self, task_id):
        from services.demo_data import is_demo_task
        if is_demo_task(task_repository.get_task(task_id)):
            raise TaskControlError("DEMO_NOT_EXECUTABLE","演示样例不启动真实生成，仅用于展示",409)

    async def cancel(self, task_id, *, key=None):
        self._reject_demo(task_id)
        digest = key_digest(key)
        try:
            result = TaskCancelled.model_validate(controls.cancel(task_id,key=digest)).model_dump()
        except TaskControlError:
            raise
        except Exception:
            raise TaskControlError("TASK_CANCEL_FAILED","取消状态持久化失败") from None
        handle = self.handles.get(task_id)
        if handle is not None:
            handle.cancel()
        return result

    async def resume(self, task_id, *, key=None):
        if self.closing:
            raise TaskControlError("TASK_SERVICE_STOPPED","任务服务正在关闭")
        self._reject_demo(task_id)
        from memory.checkpointer import resumable_mode
        digest = key_digest(key)
        try:
            mode = await asyncio.to_thread(resumable_mode,task_id)
            result,fresh = controls.resume(task_id,mode,key=digest)
            result = TaskResumed.model_validate(result).model_dump()
        except TaskControlError:
            raise
        except Exception:
            raise TaskControlError("TASK_RESUME_FAILED","任务恢复持久化失败") from None
        if result["status"] == "queued":
            self.schedule(task_id,replace=fresh)
        return result

    async def regenerate(self, task_id, *, key=None):
        if self.closing:
            raise TaskControlError("TASK_SERVICE_STOPPED","任务服务正在关闭")
        self._reject_demo(task_id)
        from services.task_regeneration import prepare_regeneration
        result = await asyncio.to_thread(prepare_regeneration,task_id,key_digest(key))
        if result is not None:
            self.schedule(result["task_id"])
        return result

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
