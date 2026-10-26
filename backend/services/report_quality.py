"""确定性质量规则：不调用模型，不把未知结果升级为通过。"""
import json
import re

# 零证据时不再调用 writer/rewriter 模型，统一输出这段确定性说明。
# 标记同时用于 rewriter 识别“无需再改写的缺口稿”。
DATA_GAP_MARKER = "本部分未获得可追溯来源，未生成结论"


def data_gap_section(task: str, topic: str | None = None) -> str:
    """生成不含事实断言的确定性数据缺口章节。"""
    subtask = task.split("子任务：", 1)[-1].strip() if "子任务：" in task else (task or "").strip()
    # 主题/子任务放在标题行：引用检查会忽略标题中的数字，避免把计划原文里的
    # 年份或“Top 5”等标签误判为报告中的数据声明。
    lines = ["## 数据缺口说明", ""]
    if topic:
        lines += [f"### 研究主题：{topic}", ""]
    if subtask:
        lines += [f"### 子任务：{subtask}", ""]
    lines += [f"{DATA_GAP_MARKER}。", "", "> 数据待补充：暂无可追溯来源。"]
    return "\n".join(lines)


def is_data_gap_draft(draft) -> bool:
    return isinstance(draft, str) and DATA_GAP_MARKER in draft


def needs_research(state: dict) -> bool:
    """仅缺证据的拒绝进入一次补研究；措辞/引用格式问题仍直接改写。"""
    if not state.get("topic") or state.get("quality_status") != "rejected":
        return False
    if state.get("research_revision_count", 0) >= 1:
        return False
    if not state.get("evidence"):
        return True
    if state.get("error_code") == "CITATION_INVALID":
        return False
    comments = "；".join(state.get("review_comments", []))
    return bool(re.search(
        r"(?:缺|不足|补充|补齐|待补充).{0,16}(?:数据|资料|证据|来源)|"
        r"(?:数据|资料|证据|来源).{0,16}(?:不足|缺失|缺乏|不相关|非相关|无关)|"
        r"(?:不相关|非相关|无关).{0,16}(?:资料|证据|来源|参考文献)", comments))


def parse_review(content: str) -> dict:
    text = (content or "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != {"passed", "comments"}:
        raise ValueError("审核字段缺失或多余")
    if type(data["passed"]) is not bool or not isinstance(data["comments"], list):
        raise ValueError("审核布尔或意见类型无效")
    if any(not isinstance(c, str) for c in data["comments"]):
        raise ValueError("审核意见必须为字符串")
    comments = [c.strip() for c in data["comments"] if c.strip()][:5]
    if not data["passed"] and not comments:
        raise ValueError("拒绝审核必须说明原因")
    return {"passed": data["passed"], "comments": comments}


def usable_draft(draft) -> bool:
    if not isinstance(draft, str) or not re.sub(r"[#>*`\s-]", "", draft):
        return False
    # 占位标记与 scheduler 的降级文案一一对应（新增占位需两边同步维护）
    return not any(marker in draft for marker in (
        "（报告草稿为空）", "（无 writer 步骤生成内容）", "（章节撰写失败：", "撰写超时，内容待补充",
    ))
