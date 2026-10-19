"""Reviewer Agent：对报告草稿进行质量审核，输出结构化意见。

A2A 协同的一环：Writer 写 `draft_content` → Reviewer 读草稿并审核，
输出 `{"passed": bool, "comments": List[str]}` 回写共享 State。
不通过时生成具体修改意见，供 Rewriter 返工（循环上限 max_iterations 次）。
"""
import json
import re

from llm.client import chat

# 审核日志统一归类到 step_id=0（A2A 阶段无 Plan 步骤，前端按序展示即可）
REVIEW_STEP_ID = 0

SYSTEM_PROMPT = """你是市场调研报告质量审核员。请对给定的报告草稿进行评审，严格按以下三条标准判断：

1. 是否包含至少 2 个具体数据点（数字、百分比、金额等）？
2. 是否引用信息来源（来源标注、机构名、报告名等）？
3. 正文字数是否不少于 1000 字？

仅返回 JSON：{"passed": true/false, "comments": ["意见1", "意见2", ...]}
- passed：满足全部 3 条为 true，否则为 false
- comments：审核意见列表；通过时可为空数组；不通过时必须给出具体修改意见（如"请补充 2024-2025 年增长率数据"）
不要输出任何其他文字，不要使用 Markdown 代码块。
"""


def parse_review(content: str) -> dict:
    """容错解析 Reviewer 的 JSON 输出。

    容错链（与 Planner 的 parse_plan 同思路）：
    1. 剥掉 ```json ... ``` 代码块包裹
    2. 截取第一个 { 到最后一个 } 的 JSON 片段
    3. 仍失败 → 返回 passed=True（放行，不阻塞整个流程）
    """
    text = (content or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"passed": True, "comments": []}   # 解析失败：放行（不阻塞流程）
    return {
        "passed": bool(data.get("passed", True)),
        "comments": [str(c).strip() for c in data.get("comments", []) if str(c).strip()][:5],
    }


async def _review(draft: str) -> dict:
    """调用 LLM 审核草稿，返回 {"passed": ..., "comments": [...]}。"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"待审核报告草稿：\n\n{draft}"},
    ]
    resp = await chat(messages)
    return parse_review(resp.choices[0].message.content or "")


async def reviewer_node(state: dict) -> dict:
    """LangGraph 节点：审核草稿 → 更新 passed / review_comments / iteration。

    达到审核上限时的输出规则：
    - 审核通过 → passed=True，路由到 END
    - 审核不通过且 iteration < max_iterations → 打回（iteration += 1），路由到 Rewriter
    - 审核不通过且 iteration >= max_iterations → **强制通过**，草稿末尾追加警告标记

    多轮审核历史（review_history）：每次审核都追加一条 {"round", "passed", "comments"}，
    供任务详情页把每轮意见分轮展示，并与 ReAct 日志中的审核小节一一对应。
    """
    draft = state.get("draft_content") or ""
    logs = list(state.get("react_logs", []))
    iteration = int(state.get("iteration", 0))
    max_iterations = int(state.get("max_iterations", 3))
    history = list(state.get("review_history", []))

    round_no = iteration + 1          # 本轮审核编号（第 1 次审核时为 1）
    logs.append(
        {"type": "Thought", "content": f"开始审核草稿（第 {round_no} 次审核）", "step_id": REVIEW_STEP_ID}
    )
    logs.append(
        {"type": "Action", "content": "llm_gen(query=按 3 条标准评审报告草稿)", "step_id": REVIEW_STEP_ID}
    )
    try:
        review = await _review(draft)
    except Exception as exc:
        # LLM 不可用：放行（不阻塞流程），记录 Observation
        review = {"passed": True, "comments": [f"审核调用失败：{exc}"]}
    passed = bool(review["passed"])
    comments = review["comments"]

    if passed:
        history.append({"round": round_no, "passed": True, "comments": comments})
        logs.append(
            {"type": "Observation", "content": f"第 {round_no} 次审核通过", "step_id": REVIEW_STEP_ID}
        )
        return {
            "passed": True,
            "review_comments": comments,
            "review_history": history,
            "react_logs": logs,
        }

    # 不通过：先 +1 再判断是否超限
    iteration += 1
    if iteration >= max_iterations:
        forced = "\n\n> ⚠️ 部分章节未通过校验（已强制输出）"
        history.append({"round": round_no, "passed": False, "comments": comments})
        logs.append(
            {
                "type": "Observation",
                "content": (
                    f"第 {round_no} 次审核不通过且已达循环上限（{iteration}/{max_iterations}），"
                    f"强制通过并标记警告；意见：{'｜'.join(comments)}"
                ),
                "step_id": REVIEW_STEP_ID,
            }
        )
        return {
            "passed": True,
            "forced_pass": True,
            "review_comments": comments,
            "review_history": history,
            "iteration": iteration,
            "draft_content": draft + forced,
            "react_logs": logs,
        }

    history.append({"round": round_no, "passed": False, "comments": comments})
    logs.append(
        {
            "type": "Observation",
            "content": (
                f"第 {round_no} 次审核不通过：{len(comments)} 条意见——"
                f"{'｜'.join(comments)}，进入第 {iteration + 1} 轮返工"
            ),
            "step_id": REVIEW_STEP_ID,
        }
    )
    return {
        "passed": False,
        "forced_pass": False,
        "review_comments": comments,
        "review_history": history,
        "iteration": iteration,
        "react_logs": logs,
    }
