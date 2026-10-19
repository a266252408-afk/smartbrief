"""SmartBrief 后端入口：单智能体、工具调用、技能池、多智能体任务编排、
A2A 审核循环、RAG 知识库与智能体记忆。"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router
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
    # 意外中断兜底（paused 状态）：上次进程退出遗留的 running 任务统一置为 paused，
    # 用户可点「恢复执行」从 SqliteSaver(checkpoints.db) 断点续跑
    try:
        interrupted = task_repository.mark_interrupted_tasks_as_paused()
        if interrupted:
            logger.warning("发现 %d 个因服务中断遗留的任务，已标记为 paused（可恢复执行）", interrupted)
    except Exception as exc:  # pragma: no cover - 真实异常路径
        logger.warning("遗留任务暂停清理失败（忽略）：%s", exc)
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
    yield
    # 关闭时回收子进程 / HTTP 连接
    await mcp_manager.close()


app = FastAPI(
    title="SmartBrief API",
    description="多智能体报告生成平台，支持研究规划、联网检索、审核返工、知识库与记忆、自动化评测。",
    version="0.1.0",
    lifespan=lifespan,
)

# 开发环境放开跨域（后续可收紧）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=True)
