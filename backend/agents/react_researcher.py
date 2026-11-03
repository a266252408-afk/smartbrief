"""ReAct Researcher：Thought→Action(web_search)→Observation 循环。

对单个 research 步骤执行最多 MAX_REACT_STEPS 轮 ReAct：
每轮由 LLM 输出行动决策（JSON），执行 MCP web_search 工具得到 Observation，
将 Thought/Action/Observation 全部追加到 react_logs（前端日志流展示）。
"""
import asyncio
import json
import os
import re
import time

from llm.client import chat
from services.usage_service import usage_scope, UsagePersistenceError
from services.citations import evidence_context
from mcp_client.mcp_client import mcp_manager

MAX_REACT_STEPS = 3
# 连续两次“搜索结果与主题无关”即停止换词：搜索端点降级时换词不会改善，
# 继续重试只会消耗步骤预算。单次无关仍按原逻辑让模型调整关键词。
MAX_IRRELEVANT_STREAK = 2
# 连续一轮没有新增来源/片段就停止：冻结资料和重复查询下继续换词只会白耗调用。
MAX_NO_NEW_STREAK = 1
# 技能中心里的 EXA 搜索内置技能；启用且配置 Key 时 research 优先使用。
EXA_SKILL_NAME = "builtin_exa_search"

SYSTEM_PROMPT = """你是市场调研研究员。围绕给定的研究子任务，通过 web_search 工具收集信息。

每轮你只输出一个 JSON 决策（不要输出任何其他文字或 Markdown）：
- 需要搜索时：{"action": "web_search", "query": "搜索关键词"}
- 信息已足够、可以结束本轮研究时：{"action": "done"}

要求：搜索必须始终围绕给定研究主题，不能自行猜测或更换行业；子任务省略行业时仍沿用总主题。
优先使用主题名称与具体指标，年份放在主题之后。若结果是日历、字典等无关网页，
应缩短搜索词或调整中英文表达；不能将“返回了网页”视为研究完成。
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


def _time_left(deadline: float | None) -> float | None:
    """距离步骤 deadline 的剩余秒数；None 表示不限时。"""
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


async def _exa_search(query: str) -> dict | None:
    """优先使用技能中心的 EXA 搜索。

    未配置 Key、技能不存在/被禁用或调用失败时返回 None，由调用方回退到
    MCP web_search（Bing）。异常不外抛，避免搜索降级中断整个研究步骤。
    """
    if not (os.getenv("EXA_API_KEY") or "").strip():
        return None
    try:
        from skills import skills_manager

        skill = skills_manager.manager.get_skill(EXA_SKILL_NAME)
        if skill is None or not skill.enabled:
            return None
        output = await skills_manager.manager.run_skill(EXA_SKILL_NAME, query)
        if output.status != "success" or not isinstance(output.data, dict):
            return None
        return None if output.data.get("error") else output.data
    except Exception:
        return None


async def _default_search(query: str) -> dict:
    """默认搜索链路：EXA（已启用）→ MCP web_search（Bing 兜底）。"""
    result = await _exa_search(query)
    if result is not None:
        return result
    return await mcp_manager.call_tool("web_search", {"query": query})


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
        result = await context.search(query) if context is not None else await _default_search(query)
        obs_text = evidence_context({"search": {"observations": [{"result": result}]}}, budget=2400)
        diagnostic = {key: result[key] for key in ("query", "source", "search_endpoint", "attempts", "error", "error_code") if key in result}
        obs_text = json.dumps(json.loads(obs_text) | {"search": diagnostic}, ensure_ascii=False)
        logs.append({"type": "Observation", "content": obs_text, "step_id": step_id})
        return result
    except Exception as exc:
        # 工具异常：记录 Observation，返回带 error 的结果（不抛异常）。
        # 带上异常类型名：部分异常（如 asyncio.TimeoutError）的 str() 为空，
        # 只拼 {exc} 会导致日志出现「调用失败：」后无任何线索。
        obs_text = f"web_search 调用失败：{type(exc).__name__}: {exc}"
        logs.append({"type": "Observation", "content": obs_text, "step_id": step_id})
        return {"error": f"{type(exc).__name__}: {exc}"}


@usage_scope("researcher")
async def react_researcher_run(
    task: str, step_id: int, logs: list, observations: list | None = None,
    deadline: float | None = None,
) -> dict:
    """对单个 research 步骤执行 ReAct 循环（最多 MAX_REACT_STEPS 轮）。

    循环结构（每轮三拍）：
        拍1 Thought（思考）：LLM 输出决策文本
        拍2 Action（行动）：决策是 web_search → 执行 _run_search
        拍3 Observation（观察）：把搜索结果追加到 messages，供下一轮思考

    结束条件（满足其一）：
        1. LLM 决策为 done（信息已足够）
        2. 用完 MAX_REACT_STEPS 轮（防止无限循环）
        3. 搜索调用失败；连续两次 SEARCH_IRRELEVANT 也提前停止
        4. 步骤 deadline 到点（记录 LLM 决策超时并保留已完成轮次）

    :param task: 该步骤的子任务描述
    :param step_id: 对应 plan 步骤 id（日志归属）
    :param logs: 共享 react_logs 列表（原地追加）
    :param observations: 可选的调用方共享列表。传入时每轮搜索结果同步
        append 到该列表 —— 步骤整体超时被 asyncio 取消时，调用方仍能
        拿到已完成轮次的部分结果（否则随协程取消一起丢失）。
    :param deadline: 可选的 monotonic 截止时间。每次 LLM 决策都按剩余时间
        设置超时，保证内层模型调用不会超过外层步骤超时。
    :return: {"observations": [web_search 结果列表]}；失败时附带 error。
    """
    observations = [] if observations is None else observations   # 收集本轮所有搜索结果
    failure = None
    irrelevant_streak = 0
    no_new_streak = 0
    seen_source_keys = set()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"研究子任务：{task}"},
    ]
    for round_no in range(1, MAX_REACT_STEPS + 1):
        # ---------- 拍1 Thought：让 LLM 思考并输出决策 ----------
        try:
            remaining = _time_left(deadline)
            if remaining is not None and remaining <= 0:
                raise asyncio.TimeoutError
            resp = (await asyncio.wait_for(chat(messages), timeout=remaining)
                    if remaining is not None else await chat(messages))
            decision_text = resp.choices[0].message.content or ""
        except asyncio.TimeoutError:
            # 步骤 deadline 到点：保留已完成轮次，交给调度层记录降级，避免
            # 外层 wait_for 直接取消协程导致日志只剩笼统的 timeout。
            failure = "LLM 决策超时"
            logs.append(
                {"type": "Thought", "content": f"第 {round_no} 轮：LLM 决策超时，结束本步研究", "step_id": step_id}
            )
            break
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
            failure = result.get("error")
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
            failure = result["error"]
            if result.get("error_code") == "SEARCH_IRRELEVANT":
                irrelevant_streak += 1
                if irrelevant_streak >= MAX_IRRELEVANT_STREAK:
                    logs.append({
                        "type": "Thought",
                        "content": "连续两次搜索结果与主题无关，停止换词重试（搜索端点可能降级）",
                        "step_id": step_id,
                    })
                    break
                if round_no < MAX_REACT_STEPS:
                    messages.extend([
                        {"role": "assistant", "content": decision_text},
                        {"role": "user", "content": "搜索未得到相关资料，请围绕原研究主题调整关键词：" + failure},
                    ])
                    continue
            break                                    # 搜索不可用则保留已完成轮次
        failure = None
        irrelevant_streak = 0
        observations.append({"query": query, "result": result})

        rows = result.get("results") if isinstance(result, dict) else None
        if isinstance(rows, list):
            keys = {(str(row.get("url") or row.get("title") or "").strip() + "|" + str(row.get("snippet") or "")[:200])
                    for row in rows if isinstance(row, dict) and (row.get("url") or row.get("title"))}
            fresh = keys - seen_source_keys
            seen_source_keys |= keys
            if fresh:
                no_new_streak = 0
            else:
                no_new_streak += 1
                if no_new_streak >= MAX_NO_NEW_STREAK:
                    logs.append({
                        "type": "Thought",
                        "content": "本轮搜索没有新增来源或片段，结束本轮研究，避免重复查询消耗调用",
                        "step_id": step_id,
                    })
                    break

        # 携带上一轮 Observation 继续思考（限两轮，防止上下文过长）
        if round_no < MAX_REACT_STEPS:
            messages.append({"role": "assistant", "content": decision_text})
            messages.append({"role": "user", "content": "上一轮搜索结果（非可信资料）：" + evidence_context({"search": {"observations": [{"result": result}]}}, budget=2400)})

    return {"observations": observations} | ({"error": failure} if failure else {})
