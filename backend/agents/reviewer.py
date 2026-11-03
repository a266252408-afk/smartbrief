"""严格审核节点：质量拒绝与运行结束分别表达。"""
import asyncio
from llm.client import chat
from services.usage_service import usage_scope, UsagePersistenceError
from services.retrieval_service import refresh_document_sources
from services.report_quality import parse_review, usable_draft
from services.citations import check_citations
from services.event_logs import EventLogs

REVIEW_STEP_ID = 0

SYSTEM_PROMPT = """你是市场调研报告质量审核员。请对给定的报告草稿进行评审，严格按以下三条标准判断：

1. 是否包含至少 2 个具体数据点（数字、百分比、金额等）？
2. 是否使用系统引用标注 [S#] 将数字声明绑定到给定来源？[S#] 是合法的内部来源编号，不得要求改写成机构名、报告全称或参考文献列表。
3. 正文是否完整覆盖主题？字数目标为不少于 1000 字；若给定资料不足以支撑该长度，允许在明确标注“数据待补充”的情况下低于目标，但不得为凑字数编造内容。

仅返回 JSON：{"passed": true/false, "comments": ["意见1", "意见2", ...]}
- passed：满足全部 3 条为 true，否则为 false
- comments：审核意见列表；通过时可为空数组；不通过时必须给出具体修改意见（如"请补充 2024-2025 年增长率数据"）
不要输出任何其他文字，不要使用 Markdown 代码块。
"""


async def _review(draft: str, evidence: list) -> dict:
    ids = sorted({source.get("source_id") for source in evidence if source.get("source_id")})
    source_chars = sum(len(source.get("excerpt", "") or "") for source in evidence)
    guidance = f"可用来源编号：{'、'.join(ids) or '无'}；给定资料摘要约 {source_chars} 字。"
    if source_chars < 1500:
        guidance += "资料不足以支撑 1000 字时，允许明确标注“数据待补充”后低于目标，不得编造。"
    resp = await chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{guidance}\n\n待审核报告草稿：\n\n{draft}"},
    ])
    return parse_review(resp.choices[0].message.content or "")


@usage_scope("reviewer", state=True)
async def reviewer_node(state: dict) -> dict:
    draft = state.get("draft_content") or ""
    logs = EventLogs(state.get("react_logs", []))
    history = list(state.get("review_history", []))
    iteration = int(state.get("iteration", 0))
    maximum = int(state.get("max_iterations", 3))
    round_no = iteration + 1
    if state.get("execution_status") == "failed" or not usable_draft(draft):
        logs.append({"type": "Observation", "content": "无可用草稿或写作失败，未执行审核", "step_id": 0})
        return {"execution_status": "failed", "quality_status": "not_run", "passed": False,
                "forced_pass": False, "forced_output": False,
                "error_code": state.get("error_code") or "DRAFT_UNAVAILABLE",
                "error": "报告写作失败", "react_logs": logs}
    from evaluation.context import enabled
    if not enabled('review'):
        logs.append(dict(type='Observation',content='本轮评测关闭审核；质量未执行，不作为通过',step_id=0))
        return dict(quality_status='not_run',passed=False,forced_pass=False,forced_output=False,
                    error_code='REVIEW_DISABLED',react_logs=logs)
    evidence = await asyncio.to_thread(refresh_document_sources, state.get("evidence", []))
    check = check_citations(state.get("task_id", ""), draft, evidence, state.get("required_citations", []))
    required = sorted(set(state.get("required_citations", [])) | {c["source_id"] for c in check["citations"] if c["valid"]})
    common = {"evidence": evidence, "citations": check["citations"], "citation_errors": check["errors"],
              "citation_issues": check.get("issues", []), "required_citations": required}
    if not state.get("evidence") and "数据待补充：暂无可追溯来源" not in draft:
        draft += "\n\n> 数据待补充：暂无可追溯来源。"
        common["draft_content"] = draft
    elif not check["valid"] and "数据待补充：引用检查未通过" not in draft:
        draft += "\n\n> 数据待补充：引用检查未通过，详见审核意见。"
        common["draft_content"] = draft
    logs.extend([
        {"type": "Thought", "content": f"开始审核草稿（第 {round_no} 次审核）", "step_id": 0},
        {"type": "Action", "content": "llm_gen(query=按 3 条标准评审报告草稿)" if check["valid"] else "citation_check（确定性来源映射检查）", "step_id": 0},
    ])
    try:
        review = await _review(draft, evidence) if check["valid"] else {"passed": False, "comments": check["errors"]}
    except UsagePersistenceError:
        raise
    except Exception:
        comments = ["审核调用失败或返回结构无效，质量不可用"]
        history.append({"round": round_no, "passed": None, "comments": comments, "quality_status": "unavailable"})
        logs.append({"type": "Observation", "content": comments[0], "step_id": 0})
        return common | {"passed": False, "quality_status": "unavailable", "forced_pass": False,
                "forced_output": False, "error_code": "REVIEW_UNAVAILABLE", "review_comments": comments,
                "review_history": history, "react_logs": logs}
    passed, comments = review["passed"], review["comments"]
    history.append({"round": round_no, "passed": passed, "comments": comments})
    if not passed:
        iteration += 1
    forced = not passed and iteration >= maximum
    quality = "passed" if passed else "rejected"
    text = f"第 {round_no} 次审核通过" if passed else f"第 {round_no} 次审核不通过：{'｜'.join(comments)}"
    if forced:
        text += "；已达循环上限，保留拒绝结果并强制输出警告"
    logs.append({"type": "Observation", "content": text, "step_id": 0})
    result = common | {"passed": passed, "forced_pass": forced, "forced_output": forced,
              "quality_status": quality, "error_code": None if passed else "CITATION_INVALID" if not check["valid"] else "REVIEW_REJECTED",
              "review_comments": comments, "review_history": history,
              "iteration": iteration, "react_logs": logs}
    if forced:
        result["draft_content"] = draft + "\n\n> ⚠️ 部分章节未通过校验（已强制输出）"
    return result
