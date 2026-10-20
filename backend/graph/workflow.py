"""真实工作流：审核通过、审核不可用、拒绝达上限或写作失败时结束。"""
import asyncio
import logging

from langgraph.graph import END, START, StateGraph

from agents.planner import planner_node
from agents.rewriter import rewriter_node
from agents.reviewer import reviewer_node
from agents.scheduler import scheduler_node
from db import task_repository
from graph.state import AgentState
from services.report_quality import usable_draft
from memory import checkpointer as cp_mod
from memory import long_term_memory

logger = logging.getLogger(__name__)


def route_after_review(state: dict) -> str:
    """终止不意味着质量通过；保留旧路由键以兼容图结构。"""
    terminal = state.get("quality_status") in {"passed", "unavailable", "not_run"} or state.get("forced_output") or state.get("execution_status") == "failed"
    return "passed" if terminal or state.get("passed") is True else "rewrite"



def build_graph(checkpointer=None):
    """构建并编译 LangGraph（带 Reviewer/Rewriter 条件循环；Checkpointer）。

    实现说明：
    - `add_conditional_edges("reviewer", 路由函数, 映射表)`：根据 state 动态决定下一步
    - 路由函数返回映射表里的 key（"passed"→END / "rewrite"→rewriter）
    - `add_edge("rewriter", "reviewer")`：返工后**循环回 Reviewer 再审核**（不重跑 Scheduler）
    - `compile(checkpointer=...)`：挂载短期记忆（SqliteSaver），task_id 作 thread_id，
      图节点执行完毕后自动写快照 → run_task(resume=True) 可断点续跑
    """
    graph = StateGraph(AgentState)
    graph.add_node("planner", planner_node)          # 节点1：拆解计划
    graph.add_node("scheduler", scheduler_node)      # 节点2：按依赖派发执行
    graph.add_node("reviewer", reviewer_node)        # 节点3（25 节）：审核草稿
    graph.add_node("rewriter", rewriter_node)        # 节点4（25 节）：按意见返工
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "scheduler")
    graph.add_edge("scheduler", "reviewer")          # 写完整稿 → 进入审核
    graph.add_conditional_edges(
        "reviewer",
        route_after_review,
        {"passed": END, "rewrite": "rewriter"},      # 通过 → 结束；不通过 → 返工
    )
    graph.add_edge("rewriter", "reviewer")           # 返工完 → 再次审核（条件循环）
    return graph.compile(checkpointer=checkpointer)


def _initial_state(task_id: str, topic: str) -> AgentState:
    """构造初始 AgentState（白板的"初始状态"）。

    所有字段都给默认值：plan 为空（等 Planner 生成）、
    日志为空、数据为空 —— 跑完图之后这些字段会被节点逐步填充。
    """
    return {
        "regenerated_from": None,
        "document_ids": [],
        "retrievals": [],
        "evidence": [],
        "citations": [],
        "citation_errors": [],
        "required_citations": [],
        "task_id": task_id,           # 任务 id（也是后续 checkpoint 的 thread_id）
        "execution_status": "running",
        "quality_status": "pending",
        "forced_output": False,
        "error_code": None,
        "topic": topic,               # 用户提交的研究主题
        "plan": [],                   # Planner 节点会写入步骤列表
        "current_step_index": 0,      # 初始进度 0
        "completed_steps": [],        # 初始无已完成步骤
        "react_logs": [],             # 初始无日志
        "research_data": {},          # 初始无研究数据
        "draft_content": "",          # 初始无草稿
        "final_report": None,         # 最终报告暂缺
        "error": None,                # 无异常
        # A2A 审核循环
        "review_comments": [],        # 初始无审核意见
        "review_history": [],         # 初始无各轮审核记录
        "iteration": 0,               # 初始循环次数 0
        "max_iterations": 3,          # 循环上限 3 次（最多返工 2 次，第 3 次仍不过则强制输出）
        "passed": None,               # 尚未审核
        "forced_pass": False,         # 未强制输出
        # 长期记忆命中：Planner 检索历史报告后填充
        "memory_hits": [],            # 初始无命中
    }


async def _persist_logs(task_id: str, react_logs: list) -> None:
    """将图内生成的 ReAct 日志批量落库（task_logs 表）。

    实现说明：图执行期间 LLM 的 Thought/Action/Observation 都存在
    state["react_logs"] 里（内存），图跑完后统一写进 SQLite。
    这样前端轮询 /tasks/{id}/status 才能查到日志流。
    """
    for log in react_logs or []:
        task_repository.append_log(
            task_id,
            step_id=log.get("step_id", 0),
            log_type=log.get("type", "Thought"),
            content=str(log.get("content", "")),
        )


async def _remember_task(task_id: str, topic: str, final_report: str) -> None:
    """任务成功后把 (topic, final_report) 写入长期记忆（容错，不阻断流程）。

    长期记忆由 ChromaDB 承担：embedding 走网络，故放进线程池；
    任何失败仅记 warning，绝不影响任务状态回写。
    """
    try:
        await asyncio.to_thread(long_term_memory.remember, topic, final_report, task_id)
    except Exception:  # pragma: no cover - 真实异常路径
        logger.warning("任务 %s 写入长期记忆不可用（报告保留）", task_id)


async def run_task(task_id: str, topic: str, resume: bool = False) -> dict:
    """执行完整流程：运行图 → 落库日志与最终状态。

    流程示意：
        1. 构造初始 state（白板）；resume=True 且有快照时改为从断点继续
        2. graph.ainvoke(initial / None)：LangGraph 自动按边执行
           planner（生成计划）→ scheduler（派发 ReAct 执行）
           → reviewer（审核草稿；A2A 不通过则返工给 rewriter 覆盖式重写，最多返工 2 次）
        3. 把图内产生的 react_logs 批量落库（task_logs 表）
        4. 把最终状态写回 tasks 表（status=completed），
           含 A2A 审核结果字段：review_comments / review_history / iteration /
           max_iterations / passed / forced_pass 及memory_hits
        5. 任务成功后调用 remember() 写入长期记忆（memory_collection）
        6. 任一环节异常：把任务标记为 failed 并记录 error

    图以 checkpointer=SqliteSaver(backend/checkpoints.db) 编译，
    task_id 作 thread_id —— 每次节点执行完自动落快照；resume=True 时从最近断点续跑
    （后端重启后仍可恢复，属"图执行到哪个节点"的框架层恢复）。

    :param task_id: 任务 id（由 API 层生成，也是 checkpoint 的 thread_id）
    :param topic: 用户提交的研究主题
    :param resume: True 表示从最近 checkpoint 恢复中断的任务（无快照则从头执行）
    :return: 最终 AgentState（dict）
    """
    initial = _initial_state(task_id, topic)
    final = {}
    try:
        from services.retrieval_service import retrieve_documents
        task = task_repository.get_task(task_id)
        initial["document_ids"] = (task or {}).get("document_ids", [])
        initial["regenerated_from"] = (task or {}).get("regenerated_from")
        # 短期记忆 Checkpointer：以 async with 进入 SqliteSaver 连接生命周期
        # （新版 langgraph-checkpoint-sqlite 的 API），compile + invoke 全程持有连接，
        # 图每次节点执行后自动向 checkpoints.db 落快照（缺依赖时降级 MemorySaver）。
        async with cp_mod.open_checkpointer() as (cp, _kind):
            graph = build_graph(checkpointer=cp)
            config = {"configurable": {"thread_id": task_id}}
            if resume and await cp_mod.async_has_checkpoint(cp, task_id):
                # 断点续跑：不传 input，LangGraph 从最近快照继续未完成的节点
                final = await graph.ainvoke(None, config)
            else:
                if initial["document_ids"]:
                    record = await asyncio.to_thread(retrieve_documents, topic, initial["document_ids"], task_id=task_id)
                    initial["retrievals"] = [record]
                    initial["evidence"] = record["evidence"]
                    task_repository.update_task(task_id, retrievals=[record], evidence=record["evidence"])
                final = await graph.ainvoke(initial, config)          # 新任务从头跑
        failed = final.get("execution_status") == "failed" or not usable_draft(final.get("draft_content"))
        final_report = None if failed else final["draft_content"]
        final.update(execution_status="failed" if failed else "completed",
                     quality_status="not_run" if failed else final.get("quality_status", "unknown"))
        await _persist_logs(task_id, final.get("react_logs", []))   # 日志落库
        task_repository.update_task(                  # 最终状态落库
            task_id,
            status=final["execution_status"],
            quality_status=final.get("quality_status", "unknown"),
            forced_output=bool(final.get("forced_output", final.get("forced_pass"))),
            error_code=final.get("error_code"),
            plan=final.get("plan", []),
            current_step_index=final.get("current_step_index", 0),
            completed_steps=final.get("completed_steps", []),
            research_data=final.get("research_data", {}),
            document_ids=final.get("document_ids", initial["document_ids"]),
            retrievals=final.get("retrievals", initial["retrievals"]),
            evidence=final.get("evidence", []),
            citations=final.get("citations", []),
            citation_errors=final.get("citation_errors", []),
            draft_content=final.get("draft_content", ""),
            final_report=final_report,
            # A2A 审核循环结果
            review_comments=final.get("review_comments", []),
            review_history=final.get("review_history", []),
            iteration=final.get("iteration", 0),
            max_iterations=final.get("max_iterations", 3),
            passed=bool(final.get("passed")),
            forced_pass=bool(final.get("forced_pass")),
            # 长期记忆命中
            memory_hits=final.get("memory_hits", []),
        )
        # 仅审核通过的报告准入新记忆；旧记忆治理留T06。
        if failed:
            raise RuntimeError(final.get("error") or "未生成有效报告")
        if final["quality_status"] == "passed":
            await _remember_task(task_id, topic, final_report)
        return final
    except Exception as exc:
        logger.exception("任务 %s 执行失败", task_id)
        task_repository.update_task(task_id, status="failed", quality_status="not_run", passed=False,
                                    forced_output=False, error_code=final.get("error_code") or "TASK_EXECUTION_FAILED", error=str(exc))  # 失败也落库，前端可展示
        raise
