"""SmartBrief 后端入口：单智能体、工具调用、技能池、多智能体任务编排、
A2A 审核循环、RAG 知识库与智能体记忆。"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router
from api.memory_routes import router as memory_router
from api.usage_routes import router as usage_router
from api.task_control_routes import router as task_control_router
from api.evaluation_routes import router as evaluation_router
from api.task_outcomes import router as task_outcome_router, knowledge_router
from api.security import authenticate, AuthSession
from services.security import cors_origins
from db import database
from db import evaluation_repository
from db import task_repository
from mcp_client.mcp_client import mcp_manager
from rag import store as rag_store
from skills.skills_manager import manager as skills_manager
from tools import builtin_tools

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化各子系统，关闭时回收 MCP 连接。

    启动顺序（任何一步失败都会阻止应用启动，便于尽早暴露配置问题）：
    业务库建表 → RAG 知识库预置 → 内置工具注册表 → 技能池 → 拉起启用的 MCP Server。

    RAG 预置：把 data/templates/（5 份模板）与 data/terminologies.txt
    （30 个术语）写入 ChromaDB rag_collection（幂等：已就绪自动跳过）。
    测试/CI 可通过环境变量 SMARTBRIEF_RAG_SEED=0 关闭（避免离线环境网络调用）。
    """
    # 初始化 SQLite 业务库（tasks / task_logs / evaluation_reports 表）
    database.init_db()
    from services.task_service import get_task_service
    from services.instance_lock import InstanceLock
    service = get_task_service()
    instance = InstanceLock(database.DB_PATH.with_suffix(".runner.lock"))
    instance.acquire()
    try:
        # 意外中断兜底（paused 状态）：上次进程退出遗留的 running 任务统一置为 paused，
        # 恢复时核对SqliteSaver待执行节点；不可续跑时明确从头重试
        try:
            interrupted = task_repository.mark_interrupted_tasks_as_paused()
            if interrupted:
                logger.warning("发现 %d 个因服务中断遗留的任务，已标记为 paused（可恢复执行）", interrupted)
        except Exception:
            raise RuntimeError("任务恢复状态不可用，服务启动已停止") from None
        # 意外中断兜底（评测）：遗留的 running 评测统一置为 failed，
        # 否则前端评测中心会永久停留在旧进度条且「运行评测」按钮被禁用
        try:
            interrupted_evals = evaluation_repository.fail_running_interrupted()
            if interrupted_evals:
                logger.warning("发现 %d 个因服务中断遗留的评测，已标记为 failed（可重新触发）", interrupted_evals)
        except Exception as exc:  # pragma: no cover - 真实异常路径
            logger.warning("遗留评测清理失败（忽略）：%s", exc)
        # 初始化 RAG 知识库预置数据（幂等；失败仅告警不阻断启动）
        if os.getenv("SMARTBRIEF_RAG_SEED", "1") == "1":
            try:
                summary = rag_store.seed_rag_knowledge()
                logger.info("RAG 知识库预置完成：%s", summary)
            except Exception as exc:
                logger.warning("RAG 知识库预置失败（可稍后重试）：%s", exc)
        # 初始化内置工具注册表（应用持久化的开关状态）
        builtin_tools.init()
        # 初始化技能池：注册内置技能 + 加载 skills_pool/ 下的自定义技能
        skills_manager.init()
        # 加载 MCP Server 配置并启动所有启用的 Server（内置 stdio 自动拉起子进程）
        await mcp_manager.start()
        service.start()
        yield
    finally:
        try:
            from services.evaluation_service import close as close_evaluation
            try:
                await close_evaluation()
            finally:
                await service.close()
        finally:
            try:
                await mcp_manager.close()
            finally:
                instance.release()



app = FastAPI(
    title="SmartBrief API",
    description="多智能体报告生成平台，支持研究规划、联网检索、审核返工、知识库与记忆、自动化评测。",
    version="0.1.0",
    lifespan=lifespan,
)

app.middleware("http")(authenticate)

# CORS位于认证外层，白名单预检不需要凭证。
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)

@app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
async def probe():
    return {"status": "ok"}


@app.get("/api/v2/auth/session")
async def auth_session(request: Request):
    from starlette.responses import JSONResponse
    return JSONResponse({"data": AuthSession(role=request.state.principal.role).model_dump(),
                         "meta": {"request_id": request.state.request_id}},
                        headers={"Cache-Control": "no-store"})


app.include_router(router)
app.include_router(task_outcome_router)
app.include_router(knowledge_router)
app.include_router(memory_router)
app.include_router(usage_router)
app.include_router(task_control_router)
app.include_router(evaluation_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=True)
