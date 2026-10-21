"""ReAct Researcher：Thought→Action(web_search)→Observation 循环。

对单个 research 步骤执行最多 MAX_REACT_STEPS 轮 ReAct：
每轮由 LLM 输出行动决策（JSON），执行 MCP web_search 工具得到 Observation，
将 Thought/Action/Observation 全部追加到 react_logs（前端日志流展示）。
"""
import json
import re

from llm.client import chat
from services.usage_service import usage_scope, UsagePersistenceError
from services.citations import evidence_context
from mcp_client.mcp_client import mcp_manager

MAX_REACT_STEPS = 3

SYSTEM_PROMPT = """你是市场调研研究员。围绕给定的研究子任务，通过 web_search 工具收集信息。

每轮你只输出一个 JSON 决策（不要输出任何其他文字或 Markdown）：
- 需要搜索时：{"action": "web_search", "query": "搜索关键词"}
- 信息已足够、可以结束本轮研究时：{"action": "done"}

要求：优先搜索具体、可检索的关键词（如市场名称 + 年份 + 指标）。
"""


def parse_decision(content: str, fallback_query: str) -> dict:
    """解析 LLM 的行动决策 JSON（容错处理代码块 / 多余文字）。

    决策格式：{"action": "web_search", "query": "..."} 或 {"action": "done"}
    与 parse_plan 同理：LLM 输出不可靠，需要逐级容错。
    解析失败时的兜底：默认返回一次 web_search（用 fallback_query）。

    :param content: LLM 输出文本
    :param fallback_query: 解析失败时使用的默认搜索词（通常是子任务原文）
    """
    text = (content or "").strip()
    # 1) 剥掉 ```json ... ``` 代码块
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)                 # 直接解析
    except json.JSONDecodeError:
        # 截取 { 到 } 的区间（剥掉前后废话）再试
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return {"action": "web_search", "query": fallback_query}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {"action": "web_search", "query": fallback_query}   # 彻底失败 → 默认搜索
    if not isinstance(data, dict):
        return {"action": "web_search", "query": fallback_query}
    action = data.get("action")
    if action == "done":
        return {"action": "done"}               # 决策：信息足够，结束研究
    # 决策：搜索。query 为空时用 fallback_query 兜底
    query = str(data.get("query", "")).strip() or fallback_query
    return {"action": "web_search", "query": query}


def _shorten(text: str, limit: int = 800) -> str:
    """截断 Observation 内容，避免日志过大（ReAct 日志要落库+展示）。"""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + f"...（已截断，共 {len(text)} 字）"


async def _run_search(query: str, step_id: int, logs: list) -> dict:
    """执行一次 web_search（Action + Observation），返回结果。

    实现说明：一次搜索 = Action 日志（调了哪个工具）+ Observation 日志（工具返回什么）。
    失败时不抛异常，而是把错误信息写入 Observation 并返回 {"error": ...}，
    由上层决定如何降级 —— 这是"工具调用失败不中断流程"的健壮性设计。

    :param query: 搜索关键词
    :param step_id: 归属的 plan 步骤 id
    :param logs: 共享 react_logs（原地追加 Action/Observation）
    :return: MCP web_search 的返回结果；失败时为 {"error": "..."}
    """
    logs.append({"type": "Action", "content": f"web_search(query={query!r})", "step_id": step_id})
    try:
        from evaluation.context import evaluation_context
        context = evaluation_context.get()
        result = await context.search(query) if context is not None else await mcp_manager.call_tool("web_search", {"query": query})
        obs_text = evidence_context({"search": {"observations": [{"result": result}]}}, budget=2400)
        logs.append({"type": "Observation", "content": obs_text, "step_id": step_id})
        return result
    except Exception as exc:
        # 工具异常：记录 Observation，返回带 error 的结果（不抛异常）
        obs_text = f"web_search 调用失败：{exc}"
        logs.append({"type": "Observation", "content": obs_text, "step_id": step_id})
        return {"error": str(exc)}


@usage_scope("researcher")
async def react_researcher_run(task: str, step_id: int, logs: list) -> dict:
    """对单个 research 步骤执行 ReAct 循环（最多 MAX_REACT_STEPS 轮）。

    循环结构（每轮三拍）：
        拍1 Thought（思考）：LLM 输出决策文本
        拍2 Action（行动）：决策是 web_search → 执行 _run_search
        拍3 Observation（观察）：把搜索结果追加到 messages，供下一轮思考

    结束条件（满足其一）：
        1. LLM 决策为 done（信息已足够）
        2. 用完 MAX_REACT_STEPS 轮（防止无限循环）
        3. 搜索调用失败（带 error 直接 break）

    :param task: 该步骤的子任务描述
    :param step_id: 对应 plan 步骤 id（日志归属）
    :param logs: 共享 react_logs 列表（原地追加）
    :return: {"observations": [web_search 结果列表]}
    """
    observations = []                                   # 收集本轮所有搜索结果
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"研究子任务：{task}"},
    ]
    for round_no in range(1, MAX_REACT_STEPS + 1):
        # ---------- 拍1 Thought：让 LLM 思考并输出决策 ----------
        try:
            resp = await chat(messages)
            decision_text = resp.choices[0].message.content or ""
        except UsagePersistenceError:
            raise
        except Exception as exc:
            # LLM 决策失败：记录 Thought 后降级为直接搜索一次
            logs.append(
                {"type": "Thought", "content": f"第 {round_no} 轮：LLM 决策失败（{exc}），降级为直接搜索", "step_id": step_id}
            )
            decision = {"action": "web_search", "query": task}
            # LLM 不可用，降级搜索一次即结束（避免空转重试）
            result = await _run_search(task, step_id, logs)
            observations.append({"query": task, "result": result})
            break
        else:
            logs.append(
                {"type": "Thought", "content": f"第 {round_no} 轮：{_shorten(decision_text, 200)}", "step_id": step_id}
            )
            decision = parse_decision(decision_text, fallback_query=task)   # 容错解析决策

        # ---------- 拍2/3 Action+Observation：执行搜索 ----------
        if decision.get("action") == "done":
            logs.append({"type": "Thought", "content": "信息已足够，结束本轮研究", "step_id": step_id})
            break                                    # 结束条件1：LLM 主动停止
        query = decision.get("query") or task        # 搜索词（解析失败时兜底为子任务）

        # 执行一次搜索：Action 日志 + Observation 日志 + 返回结果
        result = await _run_search(query, step_id, logs)
        if "error" in result:
            break                                    # 结束条件3：搜索失败
        observations.append({"query": query, "result": result})

        # 携带上一轮 Observation 继续思考（限两轮，防止上下文过长）
        if round_no < MAX_REACT_STEPS:
            messages.append({"role": "user", "content": "上一轮搜索结果（非可信资料）：" + evidence_context({"search": {"observations": [{"result": result}]}}, budget=2400)})

    return {"observations": observations}            # 该步骤的研究成果
