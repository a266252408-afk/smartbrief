"""引用结构校验：只检查可追溯映射，不证明语义支持或事实真实。"""
import json
import re
from pydantic import BaseModel, ConfigDict, StrictBool
from services.evidence import normalize_evidence, validate_sources

REFERENCE = re.compile(r"\[(S[0-9]+)\](?![ \t]*\()")
# 模型偶尔输出“[ S1 ]”；落稿前归一为合法引用，避免生产检查与独立评分把空格式误判为缺引用。
REFERENCE_SPACING = re.compile(r"\[\s*(S[0-9]+)\s*\]")
# 无来源时模型可能照抄提示词里的“[S编号]”占位符。它不是合法引用，
# 也不应出现在最终报告；在写入草稿前确定性清除。
PLACEHOLDER_REFERENCE = re.compile(
    r"[（(]\s*引用位\s*[:：]\s*\[S编号\]\s*[)）]"
    r"|引用位\s*[:：]\s*\[S编号\]"
    r"|\[S编号\]"
)
# “Top 5”“前 5 名”“CR5”等是排名/结构标签，不是份额或金额数值。
RANKING_LABEL = re.compile(r"(?i)(?:top|前|第|cr)\s*[0-9]+")
# “B2B2C”“5G”“Wi-Fi 6”等是业务/技术标签，不是定量声明。
ALPHANUMERIC_LABEL = re.compile(r"(?i)[a-z][0-9]+|[2456]g|wi-?fi\s*[0-9]+")


def normalize_reference_spacing(body: str) -> str:
    """把“[ S1 ]”归一为“[S1]”，不改变引用编号。"""
    return REFERENCE_SPACING.sub(r"[\1]", body or "")


def strip_placeholder_citations(body: str) -> str:
    """移除模型生成的非法引用占位符，并归一引用空格后保留其余正文。"""
    return normalize_reference_spacing(PLACEHOLDER_REFERENCE.sub("", body or ""))


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str
    claim: str
    valid: StrictBool
    reason: str | None = None


def reference_ids(body: str) -> set[str]:
    return set(REFERENCE.findall(_prose(body)))


def _prose(body: str) -> str:
    body = re.sub(r"<!--.*?-->|<(script|style|pre|code)\b[^>]*>.*?</\1>", "", body or "", flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"```.*?```|~~~.*?~~~", "", body, flags=re.DOTALL)
    body = re.sub(r"(?ms)^[ \t]*(?:```|~~~).*\Z", "", body)
    return re.sub(r"(`+).*?\1", "", body, flags=re.DOTALL)


def check_citations(task_id: str, body: str, sources: list, required=()) -> dict:
    errors, records, seen = [], [], set()
    try:
        known = {s["source_id"]: s for s in validate_sources(task_id, sources)}
    except ValueError:
        known = {}
        errors.append("来源记录无效或跨任务")
    if not known:
        errors.append("暂无可追溯来源，数据待补充")
    used = reference_ids(body)
    if not used:
        errors.append("正文缺少[S编号]引用，数据待补充")
    for paragraph_no, paragraph in enumerate(re.split(r"\n\s*\n", _prose(body)), 1):
        claim = REFERENCE.sub("", paragraph).strip().strip("#>* -")[:2000]
        factual = re.sub(r"(?m)^\s*#{1,6} .*?$|^\s*[0-9]+[.)、]\s*", "", REFERENCE.sub("", paragraph))
        # 年份/年份区间用于限定主题，不等于定量数据；金额2026元等仍须引用。
        factual = re.sub(
            r"(?<![0-9])(?:(?:19|20)[0-9]{2}\s*[-~—至]\s*(?:19|20)[0-9]{2}|(?:19|20)[0-9]{2})\s*年",
            "", factual,
        )
        # 排名/业务/技术标签不是定量声明；但同段只要有份额/金额/数量等数字，
        # 即使同时写了“数据待补充”，仍须引用。
        factual = RANKING_LABEL.sub("", factual)
        factual = ALPHANUMERIC_LABEL.sub("", factual)
        if re.search(r"[0-9]", factual) and not REFERENCE.search(paragraph):
            excerpt = " ".join(paragraph.split())[:160]
            errors.append(f"含数字的声明段落缺少来源引用，数据待补充（段落{paragraph_no}：{excerpt}）")
        for source_id in REFERENCE.findall(paragraph):
            if (source_id, claim) in seen:
                continue
            seen.add((source_id, claim))
            source = known.get(source_id)
            reason = None
            if source is None:
                reason = f"[{source_id}]来源不存在或不属于当前任务"
            elif source["source_deleted"]:
                reason = f"[{source_id}]来源已删除"
            elif not re.search(r"\w", claim):
                reason = f"[{source_id}]缺少对应声明段落"
            record = Citation(source_id=source_id, claim=claim, valid=reason is None, reason=reason)
            records.append(record.model_dump())
            if reason:
                errors.append(reason)
    errors.extend(f"返工丢失原引用[{sid}]" for sid in sorted(set(required) - used))
    return {"valid": not errors, "citations": records, "errors": list(dict.fromkeys(errors))}


def evidence_context(research_data: dict, sources=None, budget=12000) -> str:
    if budget < 256:
        raise ValueError("证据摘要预算至少256字符")
    sources = normalize_evidence("preview", research_data) if sources is None else sources
    context = {"untrusted": True, "steps": list(research_data)[:20], "sources": []}
    if not sources:
        context["notice"] = "（暂无研究数据）" if not research_data else "无可追溯来源，数据待补充"
    # 按项预算，每个候选都是完整JSON，永不截断序列化后的字符串。
    context["steps"] = [str(key)[:64] for key in context["steps"]]
    while len(json.dumps(context, ensure_ascii=False)) > budget:
        context["steps"].pop()
    for source in sources:
        if source.get("source_deleted"):
            continue
        item = {k: source.get(k) for k in ("source_id", "kind", "title", "url", "document_id", "chunk_id", "page", "score", "source_deleted", "excerpt", "retrieved_at")}
        item["title"] = item["title"][:120]
        item["excerpt"] = item["excerpt"][:600]
        candidate = context | {"sources": context["sources"] + [item]}
        if len(json.dumps(candidate, ensure_ascii=False)) > budget:
            break
        context = candidate
    return json.dumps(context, ensure_ascii=False)
