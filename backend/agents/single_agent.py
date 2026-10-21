"""单智能体：Function Calling 循环。

流程：
1. 组装 messages（含对话历史）
2. 调用 LLM（动态携带 已开启的内置工具 Schema + 已开启的 MCP 工具 Schema）
3. 若模型返回 tool_calls → 执行对应工具（内置函数 / MCP call_tool）
   → 将 tool 结果追加回 messages → 回到第 2 步（最多 MAX_LOOP 轮）
4. 无 tool_calls → 返回最终回答
"""
import json
import re
from types import SimpleNamespace

from llm.client import chat
from services.usage_service import usage_scope
from mcp_client.mcp_client import mcp_manager
from skills import skills_manager
from tools import builtin_tools

MAX_LOOP = 5

# 总结阶段"强制收敛"提示：部分模型在思考模式下会反复尝试调用工具，
# 该指令要求其基于已获得的搜索结果直接作答，防止无限循环。
FORCE_SUMMARY_PROMPT = (
    "请立即停止一切工具调用！直接基于以上已经获得的搜索结果和工具执行结果，"
    "用中文输出最终的市场调研回答。不要再提出任何新的搜索/抓取请求。"
    "如果信息不足，请基于已有信息给出初步结论并说明局限。"
)

SYSTEM_PROMPT = """你是 SmartBrief 智能市场调研助手。你的唯一身份和职责是基于联网搜索、计算器等工具完成市场调研、竞品分析、数据整理和结构化报告生成。

【绝对禁止】
- 在自我介绍、开场白、主动推荐或问候回复中，绝对禁止出现任何与火车、高铁、列车、12306、余票、车票、车站、车次、出行、票务相关的词汇、能力介绍或暗示。
- 即使用户只是简单问候（如"你好""在吗""介绍一下自己"），也绝对禁止主动推荐火车/票务查询能力。

【仅允许】
- 当用户明确提到要查火车票、高铁余票、列车时刻、12306 等需求时，你才提供相关帮助。

【问候时的标准话术】
当用户打招呼时，请严格按以下风格回复，只介绍市场调研能力：
"你好！我是 SmartBrief 智能市场调研助手。我可以帮你：
- 查询行业/市场/竞品数据
- 基于联网搜索整理研究信息
- 生成结构化市场调研报告
请告诉我你想研究什么主题？"

【工作要求】
- 你会主动调用可用工具获取准确信息，避免依赖训练记忆中的过时数据；回答应简洁、有数据支撑，并优先说明信息来源或置信度。
- 如果用户问题超出市场调研范围，请礼貌说明并尝试在能力范围内提供帮助。
"""

# 工具调用统计（内存存储，供"工具监控"页展示）
_STATS: dict = {}


def get_stats() -> dict:
    """返回工具调用统计。"""
    return _STATS


def _record(name: str, ok: bool) -> None:
    stat = _STATS.setdefault(name, {"count": 0, "success": 0, "failed": 0})
    stat["count"] += 1
    stat["success" if ok else "failed"] += 1


async def _enabled_schemas() -> list[dict]:
    """动态组装 LLM tools 参数：内置（已开启）+ MCP（已开启）+ 技能（已开启）。"""
    schemas = builtin_tools.list_enabled_schemas()
    schemas.extend(await mcp_manager.list_enabled_schemas())
    # 已占用的工具名集合：与内置/MCP 工具重名的技能跳过，保证 tools 内名称唯一
    used_names = {
        s["function"]["name"] for s in schemas if s.get("function") and s["function"].get("name")
    }
    schemas.extend(skills_manager.manager.list_enabled_schemas(excluded_names=used_names))
    return schemas


def _skill_banner() -> str:
    """已启用技能的"名片"（名称 + 描述），注入 system prompt 供 LLM 规划时感知。

    对应 OpenClaw"三级渐进式披露"的第一级——只给名片（轻量），不背全文。
    """
    enabled = [s for s in skills_manager.manager.list_skills() if s.get("enabled")]
    if not enabled:
        return ""
    lines = [
        "【可用技能清单】当用户请求与某项技能匹配时，请主动调用该技能（工具名即技能名，参数 query 为执行内容）："
    ]
    for s in enabled:
        lines.append(f"- {s['name']}（{s['display_name']}）：{s.get('description') or '执行该技能的任务'}")
    return "\n".join(lines)


def _build_system_prompt() -> str:
    """组装系统提示：基础身份（SYSTEM_PROMPT）+ 动态技能名片（通道二）。"""
    banner = _skill_banner()
    return f"{SYSTEM_PROMPT}\n\n{banner}" if banner else SYSTEM_PROMPT


async def _execute_tool(name: str, arguments: dict):
    """按工具名路由执行：内置函数 → 技能 → MCP 工具（加入技能路由）。"""
    tool = builtin_tools.get_tool(name)
    if tool is not None and tool.enabled:
        return await tool.handler(**arguments) if arguments else await tool.handler()
    if skills_manager.manager.get_skill(name) is not None:
        output = await skills_manager.manager.run_skill(name, str(arguments.get("query", "")))
        return output.model_dump()
    return await mcp_manager.call_tool(name, arguments)


# DSML 工具调用解析（DeepSeek V4 系列容错）
# 部分模型/网关会把工具调用以 DSML 标记文本的形式泄漏到 content 中，
# 而不是填充标准的 message.tool_calls。这里做 fallback 解析，保证 agent 循环
# 仍能识别并执行工具。
# 实测存在多种变体（竖线可多可少、可全角可 ASCII，"DSML" 关键字可能缺失）：
#   <|DSML|tool_calls> / <｜DSML｜tool_calls> / <tool_calls> / <｜｜tool_calls> / <|tool_calls>
_DSML_BAR = r"(?:\||｜)*"  # ASCII | 或全角 ｜，可零条或多条
_DSML_OPEN = rf"<\s*{_DSML_BAR}\s*(?:DSML\s*{_DSML_BAR}\s*)?"
_DSML_CLOSE = rf"</\s*{_DSML_BAR}\s*(?:DSML\s*{_DSML_BAR}\s*)?"


def _extract_dsml_tool_calls(content: str | None) -> tuple[list, str]:
    """从 assistant content 中提取 DSML 格式的工具调用。

    支持变体：
    - 全角/ASCII 竖线、单条/多条竖线、甚至无竖线
    - "DSML" 关键字可有可无
    - 外层 wrapper 为 tool_calls 或 function_calls
    - parameter 的 string="true"/"false" 属性

    返回 (tool_calls 列表, 清理掉 DSML 块后的 content)。
    """
    if not content:
        return [], content or ""

    outer_pattern = re.compile(
        rf"{_DSML_OPEN}(tool_calls|function_calls)\s*>(.*?){_DSML_CLOSE}"
        rf"(?:tool_calls|function_calls)\s*>",
        re.DOTALL,
    )
    invoke_pattern = re.compile(
        rf"{_DSML_OPEN}invoke\s+name=\"([^\"]+)\"\s*>(.*?){_DSML_CLOSE}invoke\s*>",
        re.DOTALL,
    )
    param_pattern = re.compile(
        rf"{_DSML_OPEN}parameter\s+name=\"([^\"]+)\""
        rf"(?:\s+string=\"(true|false)\")?\s*>(.*?){_DSML_CLOSE}parameter\s*>",
        re.DOTALL,
    )

    tool_calls: list = []
    cleaned = content
    for outer_match in outer_pattern.finditer(content):
        block = outer_match.group(2)
        for inv_match in invoke_pattern.finditer(block):
            name = inv_match.group(1)
            params_block = inv_match.group(2)
            arguments: dict = {}
            for pm_match in param_pattern.finditer(params_block):
                pname = pm_match.group(1)
                string_attr = (pm_match.group(2) or "true").lower()
                value_text = pm_match.group(3).strip()
                if string_attr == "false":
                    try:
                        value = json.loads(value_text)
                    except Exception:
                        value = value_text
                else:
                    value = value_text
                arguments[pname] = value

            call_id = f"dsml_call_{len(tool_calls)}"
            tool_calls.append(
                SimpleNamespace(
                    id=call_id,
                    type="function",
                    function=SimpleNamespace(
                        name=name,
                        arguments=json.dumps(arguments, ensure_ascii=False),
                    ),
                )
            )
        cleaned = cleaned.replace(outer_match.group(0), "")

    return tool_calls, cleaned.strip()


def _normalize_message(message):
    """把模型泄漏到 content 中的 DSML 工具调用解析回补。

    兼容三种情况：
    1. 完全没有标准 tool_calls，只有 DSML → 全部解析出来。
    2. 已有标准 tool_calls，content 里还夹带额外 DSML → 合并进去。
    3. 没有 DSML → 原样返回。

    同时保留 reasoning_content（DeepSeek 思考模式要求回传该字段，否则 API 400）。
    """
    dsml_calls, cleaned = _extract_dsml_tool_calls(message.content)
    if not dsml_calls:
        return message
    existing = list(message.tool_calls or [])
    return SimpleNamespace(
        content=cleaned,
        tool_calls=existing + dsml_calls,
        reasoning_content=getattr(message, "reasoning_content", None),
    )


def _to_assistant_message(message) -> dict:
    """把模型返回消息转成可回传的 assistant 消息。

    实现说明：
    - OpenAI 规范要求：tool 消息之前必须回填一条包含 tool_calls 的 assistant 消息。
    - DeepSeek 思考模式额外要求：assistant 消息的 reasoning_content 必须原样回传，
      否则 API 返回 400（"The reasoning_content must be passed back to the API"）。
    """
    msg = {
        "role": "assistant",
        "content": message.content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in message.tool_calls
        ],
    }
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning:
        msg["reasoning_content"] = reasoning
    return msg


@usage_scope("single_agent", standalone=True)
async def run(user_input: str, history: list | None = None) -> dict:
    """单智能体对话入口。

    :param user_input: 用户提问（自然语言，可触发工具调用）
    :param history: 历史消息（OpenAI 格式），可选
    :return: {"reply": 最终回答, "tool_calls": 本次工具调用记录列表}
    """
    # 过滤历史消息中可能存在的旧 system prompt，避免重复注入或被篡改
    messages = [msg for msg in (history or []) if msg.get("role") != "system"]
    # 将系统提示固定放在最前面，明确角色定位（含动态技能名片，通道二）
    messages.insert(0, {"role": "system", "content": _build_system_prompt()})
    messages.append({"role": "user", "content": user_input})
    tool_calls: list = []

    for _ in range(MAX_LOOP):
        resp = await chat(messages, tools=await _enabled_schemas())
        # 模型消息位于 choices[0].message（tool_calls 不挂在顶层响应上）
        # DeepSeek V4 等模型可能把工具调用以 DSML 文本泄漏到 content 中，
        # _normalize_message 会把这种 fallback 解析成标准 tool_calls。
        message = _normalize_message(resp.choices[0].message)

        if not message.tool_calls:
            # 模型给出最终回答
            return {"reply": message.content or "", "tool_calls": tool_calls}

        # OpenAI 规范：tool 消息之前必须回填一条包含 tool_calls 的 assistant 消息
        # DeepSeek 思考模式还需要原样回传 reasoning_content（_to_assistant_message 处理）。
        messages.append(_to_assistant_message(message))

        for call in message.tool_calls:
            name = call.function.name
            try:
                arguments = json.loads(call.function.arguments or "{}")
                result = await _execute_tool(name, arguments)
                _record(name, ok=True)
                success = True
            except Exception as exc:
                result = {"error": str(exc)}
                _record(name, ok=False)
                success = False

            tool_calls.append(
                {"name": name, "arguments": arguments, "result": result, "success": success}
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    # 走到这里说明 MAX_LOOP 轮内模型始终在要工具、没给出最终回答。
    # 与其直接返回生硬的兜底提示，不如再做几次无工具的总结调用，
    # 让模型基于已经拿到的工具结果给出最终回答，提升用户体验。
    # 注意：总结调用同样要过 _normalize_message——实测部分模型即使在
    # 无 tools 的调用里也会把工具调用以 DSML 文本泄漏到 content
    # （如 <tool_calls><invoke name="current_time">...</invoke>），
    # 不解析的话会把标记文本当最终答案原样返回。
    # 总结阶段最多尝试 3 次调用，保证收敛：
    # - 第 1 次泄漏 DSML 工具调用 → 执行它们，给模型一次补全信息的机会
    # - 第 2 次仍泄漏 → 直接返回剥离 DSML 后的文本，避免无限循环
    final_reply = ""
    for attempt in range(3):
        final_resp = await chat(messages)
        final_message = _normalize_message(final_resp.choices[0].message)
        if not final_message.tool_calls:
            final_reply = final_message.content or ""
            break
        if attempt > 0:
            # 第二次泄漏：用 user 消息强制总结 + tool_choice="none"，让模型收敛
            messages.append({"role": "user", "content": FORCE_SUMMARY_PROMPT})
            final_resp = await chat(messages, tool_choice="none")
            final_message = _normalize_message(final_resp.choices[0].message)
            final_reply = final_message.content or ""
            break

        # 总结调用仍想执行工具 → 先执行，再让它总结一次
        messages.append(_to_assistant_message(final_message))
        for call in final_message.tool_calls:
            name = call.function.name
            try:
                arguments = json.loads(call.function.arguments or "{}")
                result = await _execute_tool(name, arguments)
                _record(name, ok=True)
                success = True
            except Exception as exc:
                result = {"error": str(exc)}
                _record(name, ok=False)
                success = False

            tool_calls.append(
                {"name": name, "arguments": arguments, "result": result, "success": success}
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    if not final_reply:
        final_reply = "已达到最大工具调用轮次，请尝试简化问题后重试。"
    return {"reply": final_reply, "tool_calls": tool_calls}
