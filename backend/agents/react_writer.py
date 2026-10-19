"""ReAct Writer：Thought→Action(llm_gen)→Observation 循环。

对单个 writing 步骤调用 LLM 生成章节草稿（Action = llm_gen），
将 Thought/Action/Observation 追加到 react_logs。与 Researcher 的区别：
Writer 的行动是「调用 LLM 生成文本」而非调用外部搜索工具。

撰写前检索 RAG 知识库的**术语表**（rag_collection，terminology 类），
把相关术语定义注入 Prompt，提升报告专业度（如 YoY、渗透率等）。
"""
import asyncio
import json

from llm.client import chat
from rag import store as rag_store

SYSTEM_PROMPT = """你是市场调研报告撰写员。基于给定的研究数据与撰写子任务，输出对应的 Markdown 章节草稿。

要求：
- 直接输出正文内容（Markdown），不要输出代码块包裹
- 引用研究数据中的关键数字与事实（数据不足时明确标注「数据待补充」）
- 结构清晰，使用二级/三级标题组织
- 涉及专业指标时，准确使用参考术语定义中的表述（如 YoY、渗透率等）
"""


def _summarize_research(research_data: dict) -> str:
    """把研究数据压缩成供 Writer 引用的 Markdown 摘要。

    为什么要压缩？
    1. 研究数据是原始 JSON（多轮搜索的完整返回），直接塞给 LLM 会爆上下文
    2. 摘要按步骤分组（### step_1 / step_2 ...），让 LLM 清楚每条数据来自哪一步
    3. 每个关键字段限 300 字，防止上下文爆炸

    :param research_data: {"step_1": {"observations": [...]}, ...}
    :return: 压缩后的 Markdown 摘要文本
    """
    if not research_data:
        return "（暂无研究数据）"
    parts = []
    for key, value in research_data.items():         # 遍历每个步骤的研究结果
        parts.append(f"### {key}")                   # 用标题区分步骤
        if isinstance(value, dict) and "observations" in value:
            for obs in value["observations"]:        # 展开该步骤下的每条搜索结果
                parts.append(f"- 关键词 {obs.get('query', '')}：{json.dumps(obs.get('result', ''), ensure_ascii=False)[:300]}")
        else:
            parts.append(f"- {json.dumps(value, ensure_ascii=False)[:300]}")   # 兜底：直接截断
    return "\n".join(parts)


async def _retrieve_terms(task: str) -> list:
    """检索与撰写任务相关的 RAG 术语定义（线程池包装，便于测试 patch）。

    :return: 术语定义文本列表；RAG 不可用时返回 []（不影响撰写）
    """
    try:
        return await asyncio.to_thread(rag_store.search_rag_terms, task)
    except Exception:
        return []


async def react_writer_run(task: str, step_id: int, logs: list, research_data: dict) -> str:
    """对单个 writing 步骤执行 ReAct 循环（行动 = llm_gen）。

    与 Researcher 的区别：Writer 的"行动"不是调用外部搜索工具，
    而是直接调用 LLM 生成章节文本（llm_gen），随后把生成结果作为 Observation。

    执行步骤：
    1. 记 Thought：开始撰写 + Action：llm_gen
    2. 把研究数据压缩成摘要，拼进 user prompt
    3. 调 LLM 生成章节 Markdown
    4. LLM 失败 → 返回占位文本（不中断整个任务）
    5. 记 Observation：已生成章节草稿

    :param task: 该步骤的撰写子任务
    :param step_id: 对应 plan 步骤 id
    :param logs: 共享 react_logs 列表（原地追加）
    :param research_data: 已收集的研究数据（供引用）
    :return: 生成的章节 Markdown 文本
    """
    # ---------- 撰写前检索 RAG 术语表 ----------
    term_texts: list = await _retrieve_terms(task)

    # 日志记录 RAG 检索结果（便于前端观察术语注入情况）
    if term_texts:
        logs.append({
            "type": "Thought",
            "content": f"RAG 检索到 {len(term_texts)} 条相关术语：{'；'.join(term_texts)}",
            "step_id": step_id,
        })
    else:
        logs.append({
            "type": "Thought",
            "content": "RAG 检索：未找到相关术语定义",
            "step_id": step_id,
        })

    logs.append({"type": "Thought", "content": f"开始撰写：{task}", "step_id": step_id})
    # Action 日志：llm_gen（检索到术语时附带提示，便于前端观察 RAG 生效）
    action_content = "llm_gen(query=撰写本章节草稿)"
    if term_texts:
        action_content += f"（已注入 RAG 术语 {len(term_texts)} 条）"
    logs.append({"type": "Action", "content": action_content, "step_id": step_id})

    # 压缩研究数据 → 作为 Prompt 素材（避免上下文爆炸）
    research_summary = _summarize_research(research_data)
    # 术语定义 → 追加进 Prompt（写手据此用词更专业）
    term_block = ""
    if term_texts:
        term_block = "\n\n可参考的术语定义（撰写相关指标时请准确使用）：\n" + "\n".join(
            f"- {t}" for t in term_texts
        )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"撰写子任务：{task}\n\n可引用的研究数据：\n{research_summary}{term_block}",
        },
    ]
    try:
        resp = await chat(messages)                  # 行动：调用 LLM 生成文本
        content = resp.choices[0].message.content or ""
    except Exception as exc:
        # LLM 失败：返回占位文本 + Observation 记录（任务继续跑，不中断）
        content = f"（章节撰写失败：{exc}）"
        logs.append({"type": "Observation", "content": f"llm_gen 调用失败：{exc}", "step_id": step_id})
        return content

    logs.append(
        {
            "type": "Observation",
            "content": f"已生成章节草稿（{len(content)} 字），写入 draft_content",
            "step_id": step_id,
        }
    )
    return content                                    # 章节文本返回给 Scheduler 拼接
