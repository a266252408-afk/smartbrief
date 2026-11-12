"""Scheduler：按依赖关系派发 plan 步骤给对应执行 Agent。

线性版本：顺序取出「依赖已满足」的步骤，依次调用
ReAct Researcher / ReAct Writer 执行，并更新完成状态与数据。
（当前按依赖顺序串行派发；多 Agent 审核返工的条件循环已由workflow.py 层的条件边实现。）
"""
from services.evidence import normalize_evidence
from services.retrieval_service import refresh_document_sources
from services.event_logs import EventLogs
from services.report_quality import data_gap_section, repair_structure
from services.citations import apply_unit_scale_fixes, rebind_citations
from services.execution_context import run_before_deadline
from services.usage_service import ModelCallError

import asyncio
import time
from typing import List, Dict

from agents.react_researcher import react_researcher_run
from agents.react_writer import react_writer_run
from services.fact_binding import build_registry, locate_bindings
from services.report_plan import is_assembly_step as _is_assembly, normalize_report_plan
from services.review_policy import freeze_policy
from services.report_document import DocumentError

# 单个 Agent 步骤执行超时上限（超过则跳过该步骤，不让工作流卡死）。
# Researcher 预算按 3 轮 ReAct 估算：慢搜索可达 30s，慢模型单轮 18~27s，
# 90s 会在第 3 轮只剩十几秒，故放宽到 300s，给每轮留出完整决策时间。
# 单次 LLM 决策另由 react_researcher_run 的 deadline 约束，避免内层
# 模型超时大于外层步骤超时、导致整步被静默取消。
# Writer 需一次性生成整篇章节/报告、耗时明显更长；主备合计预算 900s，
# 故写作步骤放宽到 1000s，避免长文在主备切换时被外层误掐断。
AGENT_STEP_TIMEOUT_SECONDS = 300    # researcher 步骤超时上限；慢搜索+慢模型时 3 轮常超过 90 秒
WRITER_STEP_TIMEOUT_SECONDS = 1000  # writer 步骤超时上限；覆盖主备合计预算与长文生成
STEP_DEADLINE_MARGIN_SECONDS = 0.5  # 给外层 wait_for 留出收尾/落日志时间


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


def _assemble_sections(plan, sections):
    chunks = []
    by_id = {str(s['id']): s for s in plan}
    # 按实际完成顺序组装，不能假设模型给出的ID天然是拓扑顺序。
    for step_id, content in sections.items():
        step = by_id.get(str(step_id))
        if step is None:
            continue
        if _is_assembly(step, plan) and '撰写超时' not in content and '撰写失败' not in content:
            chunks = [content]
        else:
            chunks.append(content)
    return '\n\n'.join(chunks)


async def scheduler_node(state: dict, *, single_step: bool = False) -> dict:
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
    plan = normalize_report_plan(state.get("plan", []))
    completed: List[int] = list(state.get("completed_steps", []))   # 已完成步骤（会逐步累加）
    logs: List[Dict] = EventLogs(state.get("react_logs", []))       # 日志在产生时记录时间
    research_data: Dict = dict(state.get("research_data", {}))      # 研究数据（逐步填充）
    evidence = list(state.get("evidence", []))
    sections = dict(state.get("draft_sections", {}))
    bound = state.get("report_pipeline_version", 1) >= 2
    bindings = list(state.get("claim_bindings", []))
    document=state.get('report_document')
    writer_attempts=list(state.get('writer_attempts',[]))

    # 逐轮派发：每轮取所有就绪步骤；线性版串行执行（当前串行执行）
    pending = True
    while pending:
        ready = _ready_steps(plan, completed)     # 1. 找出本轮可执行的步骤
        if single_step:
            ready = ready[:1]  # 图中每次只执行一个Agent单元，返回后由检查点持久化。
        if not ready:
            pending = False                       # 没有就绪步骤 → 全部完成，退出循环
            break
        for step in ready:                        # 2. 逐个派发执行
            step_id = step["id"]
            agent = step.get("agent", "researcher")
            task = step.get("task", "")
            if state.get("topic"):
                task = f"研究主题：{state['topic']}\n子任务：{task}"
            degraded = False
            _append_log(logs, step_id, "Thought", f"调度：步骤 {step_id} 派发给 {agent}")
            if agent == "researcher":
                # 研究步骤：ReAct 循环（Thought→Action(web_search)→Observation）
                # 容错：整个步骤限时，超时则降级（记录已完成的轮次，不卡死工作流）
                step_observations: List[Dict] = []   # 共享容器：协程被取消后仍持有已完成轮次
                deadline = time.monotonic() + AGENT_STEP_TIMEOUT_SECONDS - STEP_DEADLINE_MARGIN_SECONDS
                try:
                    result = await asyncio.wait_for(
                        run_before_deadline(react_researcher_run(
                            task, step_id, logs, observations=step_observations, deadline=deadline
                        ), deadline),
                        timeout=AGENT_STEP_TIMEOUT_SECONDS,
                    )
                    research_data[f"step_{step_id}"] = result   # 研究结果按步骤号归档
                except asyncio.TimeoutError:
                    degraded = True
                    # 超时降级：保留已完成轮次的 observations（normalize_evidence
                    # 会照常注册为证据），只补 skipped 标记；流程不中断、不重试
                    _append_log(
                        logs, step_id, "Observation",
                        f"步骤 {step_id} 执行超时（>{AGENT_STEP_TIMEOUT_SECONDS}s），"
                        f"已保留 {len(step_observations)} 轮已完成搜索结果",
                    )
                    research_data[f"step_{step_id}"] = {
                        "observations": step_observations,
                        "skipped": True,
                        "error": f"timeout>{AGENT_STEP_TIMEOUT_SECONDS}s",
                    }
                except ModelCallError as exc:
                    if not getattr(exc, "step_budget_limited", False):
                        raise
                    degraded = True
                    _append_log(
                        logs, step_id, "Observation",
                        f"步骤 {step_id} 模型调用被步预算截断，"
                        f"已保留 {len(step_observations)} 轮已完成搜索结果",
                    )
                    research_data[f"step_{step_id}"] = {
                        "observations": step_observations,
                        "skipped": True,
                        "error": "step_budget_timeout",
                    }
                degraded = degraded or bool(research_data[f"step_{step_id}"].get("error"))
            else:
                evidence = await asyncio.to_thread(refresh_document_sources, evidence)
                evidence = normalize_evidence(state.get("task_id", ""), research_data, evidence)
                # 撰写步骤：带研究数据生成章节草稿（整稿生成耗时更长，用独立放宽后的超时）。
                # 零证据时不调用模型：模型在没有来源时容易把通用常识写成具体断言，
                # 既无法追溯也浪费时间。改为确定性缺口说明，仍走
                # completed/rejected/forced_output，不静默改为 failed。
                if not evidence:
                    degraded = True
                    _append_log(
                        logs, step_id, "Observation",
                        f"步骤 {step_id} 无可用证据，跳过模型撰写并生成数据缺口说明",
                    )
                    sections[str(step_id)] = data_gap_section(task, state.get("topic"))
                else:
                    try:
                        assembly_input = ({'existing_draft': _assemble_sections(plan, sections)}
                                          if _is_assembly(step, plan) else {})
                        binding_context = {"task_id": state["task_id"], "claims": []}
                        if state.get('report_pipeline_version') == 3:
                            binding_context.update(pipeline_version=3,document=document)
                        if bound:
                            binding_context["review_policy"] = freeze_policy(state)
                        if bound:
                            assembly_input["binding_context"] = binding_context
                        section = await asyncio.wait_for(
                            run_before_deadline(
                                react_writer_run(task, step_id, logs, research_data, evidence=evidence, **assembly_input),
                                time.monotonic() + WRITER_STEP_TIMEOUT_SECONDS - STEP_DEADLINE_MARGIN_SECONDS),
                            timeout=WRITER_STEP_TIMEOUT_SECONDS,
                        )
                        sections[str(step_id)] = section
                        if bound:
                            bindings.extend(binding_context["claims"])
                            if state.get('report_pipeline_version') == 3:
                                document=binding_context['document']
                                writer_attempts.extend(binding_context['attempts'])
                        if assembly_input:
                            _append_log(logs, step_id, 'Observation', '报告装配：已提供现有正文，整稿替换先前章节拼接结果；原始章节保留')
                    except asyncio.TimeoutError:
                        degraded = True
                        # 超时降级：追加占位章节，保证后续拼接 / 审核不因缺章节而崩
                        _append_log(
                            logs, step_id, "Observation",
                            f"步骤 {step_id} 执行超时（>{WRITER_STEP_TIMEOUT_SECONDS}s），已跳过",
                        )
                        # 占位文案与 Rewriter 的"草稿为空/仅占位则重写"约定呼应，改动需两边同步
                        sections[str(step_id)] = f"（步骤 {step_id} 撰写超时，内容待补充）"
                    except DocumentError as exc:
                        degraded=True
                        writer_attempts.extend(getattr(exc,'attempts',[]))
                        sections[str(step_id)]='（章节撰写失败：结构化报告未获验证）'
                        _append_log(logs,step_id,'Observation',str(exc))
                    except ModelCallError as exc:
                        if not getattr(exc, "step_budget_limited", False):
                            raise
                        degraded = True
                        _append_log(
                            logs, step_id, "Observation",
                            f"步骤 {step_id} 写作模型调用被步预算截断，已跳过",
                        )
                        sections[str(step_id)] = f"（步骤 {step_id} 撰写超时，内容待补充）"
            completed.append(step_id)             # 3. 标记完成，解锁后续依赖
            ending = "已结束（降级，详见前述错误或超时记录）" if degraded else "完成"
            _append_log(logs, step_id, "Observation", f"步骤 {step_id} {ending}")
        if single_step:
            break

    # 组装草稿：writer 步骤的章节按执行顺序拼接成完整 Markdown
    draft_content = _assemble_sections(plan, sections)
    if not draft_content:
        # 没有 writer 步骤（或全部失败）时给出占位说明
        draft_content = "（无 writer 步骤生成内容）"
    evidence_items = normalize_evidence(state.get("task_id", ""), research_data, evidence)
    scaled = ({"body": draft_content, "fixed": []} if bound else apply_unit_scale_fixes(draft_content, evidence_items))
    if scaled["fixed"]:
        _append_log(logs, 0, "Observation", f"写作后确定性单位换算 {len(scaled['fixed'])} 处")
    draft_content = scaled["body"]
    repaired = ({"body": draft_content, "rebound": 0} if bound else rebind_citations(draft_content, evidence_items))
    if repaired["rebound"]:
        _append_log(logs, 0, "Observation", f"写作后确定性重挂引用 {repaired['rebound']} 处")
    draft_content = repair_structure(repaired["body"], state.get("topic"))
    if state.get('report_pipeline_version') != 3:
        from services.citations import repair_citation_groups
        draft_content, grouping_fixed = repair_citation_groups(draft_content, evidence_items)
        if grouping_fixed:
            _append_log(logs, 0, "Observation", f"引用分组确定性修正 {grouping_fixed} 处")
    if state.get('report_pipeline_version') == 3 and document:
        from services.report_document import render_document
        draft_content=render_document(document)

    return {
        **({'report_document':document,'writer_attempts':writer_attempts} if state.get('report_pipeline_version') == 3 else {}),
        **({"review_policy": freeze_policy(state)} if bound else {}),
        "fact_registry": build_registry(
            state.get("task_id", ""), evidence_items) if bound else state.get("fact_registry", {}),
        "claim_bindings": locate_bindings(draft_content, bindings),
        "plan": plan,
        "draft_sections": sections,
        "completed_steps": completed,             # 已完成步骤 id 列表
        "research_data": research_data,           # 各步骤研究结果（供前端/Writer 引用）
        "evidence": evidence_items,
        "draft_content": draft_content,           # 拼接后的报告草稿
        "current_step_index": len(completed),     # 进度 = 已完成步骤数
        "react_logs": logs,                       # 日志流
    }


async def scheduler_step_node(state: dict) -> dict:
    """一个研究子任务或章节为一次图提交，恢复只重做未完成单元。"""
    return await scheduler_node(state, single_step=True)


def route_after_step(state: dict) -> str:
    """还有就绪子任务时继续调度，否则沿用原审核入口。"""
    return "scheduler" if _ready_steps(state.get("plan", []), state.get("completed_steps", [])) else "reviewer"
