"""Scheduler：按依赖关系派发 plan 步骤给对应执行 Agent。

线性版本：顺序取出「依赖已满足」的步骤，依次调用
ReAct Researcher / ReAct Writer 执行，并更新完成状态与数据。
（当前按依赖顺序串行派发；多 Agent 审核返工的条件循环已由workflow.py 层的条件边实现。）
"""
from services.evidence import normalize_evidence

import asyncio
from typing import List, Dict

from agents.react_researcher import react_researcher_run
from agents.react_writer import react_writer_run

# 单个 Agent 步骤执行超时上限（超过则跳过该步骤，不让工作流卡死）。
# Researcher 走联网搜索、单步较快；Writer 需一次性生成整篇章节/报告、耗时明显更长。
# 若 Writer 沿用同一较短超时会被误掐断（草稿退化为占位符，后续 A2A 返工极易跑题），故单独放宽。
AGENT_STEP_TIMEOUT_SECONDS = 30     # researcher 步骤超时上限
WRITER_STEP_TIMEOUT_SECONDS = 120   # writer 步骤超时上限（撰写整稿需要更长时间）


def _ready_steps(plan: List[Dict], completed: List[int]) -> List[Dict]:
    """返回所有"依赖已满足、尚未完成"的步骤（按 id 升序，保证确定性）。

    就绪判定规则：
    - 步骤的 id 不在 completed 中（还没执行过）
    - 步骤 depends_on 里的每个依赖 id 都在 completed 中（依赖全完成）

    为什么按 id 升序？保证多次调度结果一致（确定性），
    避免 LLM 计划里步骤乱序导致执行顺序不稳定。
    """
    completed_set = set(completed)               # 转 set 加速"依赖是否已完成"判断
    ready = []
    for step in plan:
        if step["id"] in completed_set:
            continue                             # 已完成的步骤跳过
        # all(...)：depends_on 为空列表时返回 True（无依赖 → 立即就绪）
        if all(dep in completed_set for dep in step.get("depends_on", [])):
            ready.append(step)
    return sorted(ready, key=lambda s: s["id"])  # 按 id 升序，保证确定性


def _append_log(logs: List[Dict], step_id: int, log_type: str, content: str) -> None:
    """追加日志（统一入口，保证每条日志都带 step_id 归类）。"""
    logs.append({"type": log_type, "content": content, "step_id": step_id})


async def scheduler_node(state: dict) -> dict:
    """LangGraph 节点：调度执行所有可执行步骤。

    核心思想：**反复"取就绪步骤 → 派发执行 → 更新完成状态"**，
    直到没有可执行步骤为止（类似 while 有活干就派活）。

    派发规则：
    - researcher 步骤 → 调 react_researcher_run（ReAct 搜索循环）
    - writer 步骤 → 调 react_writer_run（基于研究数据撰写章节）
    每完成一个步骤，就把它的 id 加入 completed_steps，
    下一轮就绪判定会解锁依赖它的后续步骤。

    :param state: AgentState（含 plan / completed_steps / research_data / draft_content）
    :return: state 增量（completed_steps、research_data、draft_content、current_step_index、react_logs）
    """
    plan = state.get("plan", [])
    completed: List[int] = list(state.get("completed_steps", []))   # 已完成步骤（会逐步累加）
    logs: List[Dict] = list(state.get("react_logs", []))            # 日志流（原地追加）
    research_data: Dict = dict(state.get("research_data", {}))      # 研究数据（逐步填充）
    evidence = list(state.get("evidence", []))
    drafts: List[str] = []                                          # writer 步骤产出的章节（暂存）

    # 逐轮派发：每轮取所有就绪步骤；线性版串行执行（当前串行执行）
    pending = True
    while pending:
        ready = _ready_steps(plan, completed)     # 1. 找出本轮可执行的步骤
        if not ready:
            pending = False                       # 没有就绪步骤 → 全部完成，退出循环
            break
        for step in ready:                        # 2. 逐个派发执行
            step_id = step["id"]
            agent = step.get("agent", "researcher")
            _append_log(logs, step_id, "Thought", f"调度：步骤 {step_id} 派发给 {agent}")
            if agent == "researcher":
                # 研究步骤：ReAct 循环（Thought→Action(web_search)→Observation）
                # 容错：整个步骤限时，超时则跳过（记录降级结果，不卡死工作流）
                try:
                    result = await asyncio.wait_for(
                        react_researcher_run(step.get("task", ""), step_id, logs),
                        timeout=AGENT_STEP_TIMEOUT_SECONDS,
                    )
                    research_data[f"step_{step_id}"] = result   # 研究结果按步骤号归档
                except asyncio.TimeoutError:
                    # 超时降级：写入空占位研究结果（observations=[] + skipped 标记），
                    # 流程不中断、不重试，依赖本步骤的 writer 会收到"数据缺失"信号
                    _append_log(
                        logs, step_id, "Observation",
                        f"步骤 {step_id} 执行超时（>{AGENT_STEP_TIMEOUT_SECONDS}s），已跳过",
                    )
                    research_data[f"step_{step_id}"] = {
                        "observations": [],
                        "skipped": True,
                        "error": f"timeout>{AGENT_STEP_TIMEOUT_SECONDS}s",
                    }
            else:
                evidence = normalize_evidence(state.get("task_id", ""), research_data, evidence)
                # 撰写步骤：带研究数据生成章节草稿（整稿生成耗时更长，用独立放宽后的超时）
                try:
                    section = await asyncio.wait_for(
                        react_writer_run(step.get("task", ""), step_id, logs, research_data, evidence=evidence),
                        timeout=WRITER_STEP_TIMEOUT_SECONDS,
                    )
                    drafts.append(section)            # 章节暂存，最后统一拼接
                except asyncio.TimeoutError:
                    # 超时降级：追加占位章节，保证后续拼接 / 审核不因缺章节而崩
                    _append_log(
                        logs, step_id, "Observation",
                        f"步骤 {step_id} 执行超时（>{WRITER_STEP_TIMEOUT_SECONDS}s），已跳过",
                    )
                    # 占位文案与 Rewriter 的"草稿为空/仅占位则重写"约定呼应，改动需两边同步
                    drafts.append(f"（步骤 {step_id} 撰写超时，内容待补充）")
            completed.append(step_id)             # 3. 标记完成，解锁后续依赖
            _append_log(logs, step_id, "Observation", f"步骤 {step_id} 完成")

    # 组装草稿：writer 步骤的章节按执行顺序拼接成完整 Markdown
    draft_content = "\n\n".join(drafts)
    if not draft_content:
        # 没有 writer 步骤（或全部失败）时给出占位说明
        draft_content = "（无 writer 步骤生成内容）"

    return {
        "completed_steps": completed,             # 已完成步骤 id 列表
        "research_data": research_data,           # 各步骤研究结果（供前端/Writer 引用）
        "evidence": normalize_evidence(state.get("task_id", ""), research_data, evidence),
        "draft_content": draft_content,           # 拼接后的报告草稿
        "current_step_index": len(completed),     # 进度 = 已完成步骤数
        "react_logs": logs,                       # 日志流
    }
