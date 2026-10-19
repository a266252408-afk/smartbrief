"""内置技能：搜索通过 MCP 执行，分析、写作与审核通过 LLM 执行。

执行失败返回 failed 与错误信息；内置技能只读，支持启用或禁用。
"""
from llm import client as llm_client
from mcp_client.mcp_client import mcp_manager
from skills.base_skill import BaseSkill, SkillInput, SkillOutput

# MCP 内置搜索工具名（web_search_server.py 注册）
WEB_SEARCH_TOOL = "web_search"


async def _call_llm(system: str, query: str) -> str:
    """调用 LLM 并返回纯文本回复；异常时抛出，由上层捕获。"""
    resp = await llm_client.chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": query},
        ]
    )
    return (resp.choices[0].message.content or "").strip()


class SearchSkill(BaseSkill):
    """通过 MCP 搜索网络资料；接口异常或返回错误时报告失败。"""

    name = "builtin_search"
    display_name = "搜索"
    description = "联网搜索互联网，返回标题、摘要与链接列表"
    category = "search"
    version = "1.0.0"
    builtin = True

    async def execute(self, input: SkillInput) -> SkillOutput:
        try:
            result = await mcp_manager.call_tool(
                WEB_SEARCH_TOOL, {"query": input.query}
            )
            if not isinstance(result, dict):
                raise ValueError("搜索工具返回格式无效")
            if result.get("error"):
                raise RuntimeError(str(result["error"]))
            results = result.get("results", [])
            return SkillOutput(
                status="success",
                data={"query": input.query, "results": results},
                metadata={"mode": "web", "source": result.get("source", "mcp")},
            )
        except Exception as exc:
            return SkillOutput(
                status="failed", data=None, metadata={"error": str(exc), "mode": "web"}
            )


class AnalyzeSkill(BaseSkill):
    """真实分析：LLM 对输入内容输出结构化分析结论。"""

    name = "builtin_analyze"
    display_name = "分析"
    description = "对输入内容进行分析，输出结论与要点"
    category = "analysis"
    version = "1.0.0"
    builtin = True

    async def execute(self, input: SkillInput) -> SkillOutput:
        system = (
            "你是一位数据分析师。请分析用户给出的内容，输出：\n"
            '1. conclusion：一句话总结结论\n'
            '2. points：3-5 条要点\n'
            "仅返回 JSON：{\"conclusion\": \"...\", \"points\": [...]}"
        )
        try:
            text = await _call_llm(system, input.query)
            data = _parse_json_object(text, fallback=input.query)
            return SkillOutput(
                status="success",
                data={"query": input.query, **data},
                metadata={"mode": "llm"},
            )
        except Exception as exc:
            return SkillOutput(
                status="failed", data=None, metadata={"error": str(exc), "mode": "llm"}
            )


class WriteSkill(BaseSkill):
    """真实写作：LLM 根据主题生成结构化草稿。"""

    name = "builtin_write"
    display_name = "写作"
    description = "根据主题生成一份结构化草稿"
    category = "writing"
    version = "1.0.0"
    builtin = True

    async def execute(self, input: SkillInput) -> SkillOutput:
        system = (
            "你是一位专业写作者。请根据用户主题生成一份结构化草稿，"
            "包含标题、引言、正文要点与结语。"
        )
        try:
            draft = await _call_llm(system, input.query)
            return SkillOutput(
                status="success",
                data={"query": input.query, "draft": draft},
                metadata={"mode": "llm"},
            )
        except Exception as exc:
            return SkillOutput(
                status="failed", data=None, metadata={"error": str(exc), "mode": "llm"}
            )


class ReviewSkill(BaseSkill):
    """真实审核：LLM 对内容评审并输出分数与修改建议。"""

    name = "builtin_review"
    display_name = "审核"
    description = "对内容进行评审，输出分数与修改建议"
    category = "review"
    version = "1.0.0"
    builtin = True

    async def execute(self, input: SkillInput) -> SkillOutput:
        system = (
            "你是一位资深评审专家。请对用户内容评审，输出：\n"
            '1. score：0-100 整数\n'
            '2. suggestions：3-5 条具体修改建议\n'
            "仅返回 JSON：{\"score\": 0, \"suggestions\": [...]}"
        )
        try:
            text = await _call_llm(system, input.query)
            data = _parse_json_object(text, fallback=input.query)
            return SkillOutput(
                status="success",
                data={"query": input.query, **data},
                metadata={"mode": "llm"},
            )
        except Exception as exc:
            return SkillOutput(
                status="failed", data=None, metadata={"error": str(exc), "mode": "llm"}
            )


def _parse_json_object(text: str, fallback: str) -> dict:
    """从 LLM 文本中提取 JSON 对象；解析失败返回包含 fallback 的最小结构。"""
    import json
    import re

    stripped = text.strip()
    # 去掉 ```json ... ``` 代码块围栏
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.S)
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        # 尝试截取第一个 { ... } 片段
        start, end = stripped.find("{"), stripped.rfind("}")
        if start != -1 and end > start:
            try:
                obj = json.loads(stripped[start : end + 1])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
    return {"conclusion": fallback, "points": [], "score": 0, "suggestions": []}


# 内置技能列表（启动时逐个注册）
BUILTIN_SKILLS = [SearchSkill, AnalyzeSkill, WriteSkill, ReviewSkill]
