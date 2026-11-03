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
# 日期、季度、期号只限定统计期，不是定量声明；避免“截至2026年10月31日”被误判为缺引用。
DATE_LABEL = re.compile(
    r"(?:(?:19|20)\d{2}\s*[-~—至]\s*(?:19|20)\d{2}\s*年?"
    r"|(?:19|20)\d{2}\s*[Qq][1-4]"
    r"|(?:19|20)\d{2}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"
    r"|(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月"
    r"|(?:19|20)\d{2}\s*[-/]\s*\d{1,2}"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|第\s*[一二三四五六七八九十0-9]+\s*季度"
    r"|[Qq][1-4]"
    r"|(?:19|20)\d{2}\s*年)"
)
# 有序列表编号、章节编号不是定量声明。
ORDER_LABEL = re.compile(r"(?m)^\s*\d+\s*[.)、]\s*")
SECTION_LABEL = re.compile(r"第\s*\d+\s*[部分章节条款]")
# 提取定量 token（数字 + 常见单位）；日期和标签先被移除。
QUANTITY = re.compile(
    r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?\s*(?:%|％|亿|万|千|百|元|美元|台|辆|人|个|件|家|座|GW|GWh|MW|MWh|kW|kWh|吨|平方米|公里|km|℃|倍|次)?",
    re.IGNORECASE,
)
# 只有带单位/小数或至少 3 位数字的 token 才算定量声明；裸“3”多半是编号。
QUANTITY_SUBSTANTIVE = re.compile(r"[%％亿万千百元台辆人个件家座GWkWh吨公里倍次]|\.|[0-9]{3}", re.IGNORECASE)


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


def _factual_text(paragraph: str) -> str:
    """移除标题、编号、日期、排名和技术标签，只留可能构成定量声明的文本。"""
    factual = REFERENCE.sub("", _prose(paragraph))
    factual = re.sub(r"(?m)^\s*#{1,6} .*?$", "", factual)
    factual = ORDER_LABEL.sub("", factual)
    factual = DATE_LABEL.sub("", factual)
    factual = RANKING_LABEL.sub("", factual)
    factual = ALPHANUMERIC_LABEL.sub("", factual)
    return SECTION_LABEL.sub("", factual)


def quantitative_tokens(paragraph: str) -> list[str]:
    return [token for token in (match.group(0).strip() for match in QUANTITY.finditer(_factual_text(paragraph)))
            if QUANTITY_SUBSTANTIVE.search(token)]


def _number_parts(tokens: list[str]) -> list[str]:
    numbers = []
    for token in tokens:
        match = re.match(r"\d+(?:\.\d+)?", token)
        if match:
            numbers.append(match.group(0))
    return numbers


def _number_in_text(number: str, text: str) -> bool:
    return re.search(r"(?<![\d.])" + re.escape(number) + r"(?![\d])", text or "") is not None


def _candidate_ids(tokens: list[str], sources) -> list[str]:
    """返回摘要中同时包含全部数字的来源编号；无唯一候选时由调用方决定。"""
    numbers = _number_parts(tokens)
    if not numbers:
        return []
    return sorted({source.get("source_id") for source in sources
                   if source.get("source_id") and not source.get("source_deleted")
                   and all(_number_in_text(number, source.get("excerpt", "")) for number in numbers)})


def attach_citations(body: str, sources) -> dict:
    """对“数字唯一对应一个来源”的段落做确定性补引用。

    只处理当前没有引用、且全部数字都能在同一来源摘要中找到的段落；
    多个来源同时匹配或没有匹配时保留原样，交给 Rewriter/人工处理。
    """
    parts = re.split(r"(\n\s*\n)", body or "")
    attached, issues = 0, []
    for index in range(0, len(parts), 2):
        paragraph = parts[index]
        if not paragraph.strip() or REFERENCE.search(paragraph):
            continue
        if re.fullmatch(r"\s*#{1,6} .*", paragraph.strip()):
            continue
        tokens = quantitative_tokens(paragraph)
        if not tokens:
            continue
        candidates = _candidate_ids(tokens, sources)
        if len(candidates) == 1:
            stripped = paragraph.rstrip()
            parts[index] = stripped + f" [{candidates[0]}]" + paragraph[len(stripped):]
            attached += 1
        else:
            issues.append(dict(paragraph=index // 2 + 1, excerpt=" ".join(paragraph.split())[:160],
                               tokens=tokens, candidates=candidates))
    return {"body": "".join(parts), "attached": attached, "issues": issues}


def check_citations(task_id: str, body: str, sources: list, required=()) -> dict:
    errors, records, seen, issues = [], [], set(), []
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
        # 年份、日期、季度、排名/业务/技术标签不是定量声明；但同段只要有份额/金额/数量等数字，
        # 即使同时写了“数据待补充”，仍须引用。
        tokens = quantitative_tokens(paragraph)
        if tokens and not REFERENCE.search(paragraph):
            excerpt = " ".join(paragraph.split())[:160]
            candidates = _candidate_ids(tokens, known.values())
            issues.append(dict(paragraph=paragraph_no, excerpt=excerpt, tokens=tokens, candidates=candidates))
            hint = "；候选来源：" + "、".join(candidates) if candidates else "；无唯一候选来源"
            errors.append(f"含数字的声明段落缺少来源引用，数据待补充（段落{paragraph_no}：{excerpt}{hint}）")
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
    return {"valid": not errors, "citations": records, "errors": list(dict.fromkeys(errors)), "issues": issues}


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
