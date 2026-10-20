"""确定性质量规则：不调用模型，不把未知结果升级为通过。"""
import json
import re


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
    return not any(marker in draft for marker in (
        "（报告草稿为空）", "（无 writer 步骤生成内容）", "（章节撰写失败：", "撰写超时，内容待补充",
    ))
