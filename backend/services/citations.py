"""引用结构校验：只检查可追溯映射，不证明语义支持或事实真实。"""
import json
import re
from pydantic import BaseModel, ConfigDict, StrictBool
from services.evidence import normalize_evidence, validate_sources

REFERENCE = re.compile(r"\[(S[0-9]+)\](?![ \t]*\()")


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
    for paragraph in re.split(r"\n\s*\n", _prose(body)):
        claim = REFERENCE.sub("", paragraph).strip().strip("#>* -")[:2000]
        factual = re.sub(r"(?m)^\s*#{1,6} .*?$|^\s*[0-9]+[.)、]\s*", "", REFERENCE.sub("", paragraph))
        if re.search(r"[0-9]", factual) and not REFERENCE.search(paragraph) and "待补充" not in paragraph:
            errors.append("含数字的声明段落缺少来源引用，数据待补充")
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
        item = {k: source.get(k) for k in ("source_id", "title", "url", "excerpt", "retrieved_at")}
        item["title"] = item["title"][:120]
        item["excerpt"] = item["excerpt"][:600]
        candidate = context | {"sources": context["sources"] + [item]}
        if len(json.dumps(candidate, ensure_ascii=False)) > budget:
            break
        context = candidate
    return json.dumps(context, ensure_ascii=False)
