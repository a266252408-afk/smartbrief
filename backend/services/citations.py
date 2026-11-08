"""引用结构校验：只检查可追溯映射，不证明语义支持或事实真实。"""
import json
import re
from pydantic import BaseModel, ConfigDict, StrictBool
from services.evidence import normalize_evidence, validate_sources
from services.evidence_ranking import evidence_item, rank_sources, required_evidence_context
from services.numbers import (
    numbers_match,
    parse_number_values,
    scale_conversion,
    token_matches_text,
)

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
# 标准编号（GB 12021.2-2025、GB/T 8059、IEC 62552）不是定量声明。
STANDARD_LABEL = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:GB|IEC|ISO|EN|UL|CSA)\s*/?\s*T?\s*\d+(?:\.\d+)*(?:[-—:]\d{1,4})*"
)
# 有序列表编号不是定量声明；点号后紧跟数字的（79.4%）是小数，不能当列表编号。
ORDER_LABEL = re.compile(r"(?m)^\s*\d+\s*[.)、](?!\d)\s*")
SECTION_LABEL = re.compile(r"第\s*[一二三四五六七八九十0-9]+\s*[部分章节条款]")
# 提取定量 token（数字 + 常见单位）；日期和标签先被移除。
# 千分位与 K/W/L 价格带必须整体识别，否则“1,271亿”会被拆成“271亿”。
QUANTITY = re.compile(
    r"(?<![A-Za-z0-9.])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?:万亿|千亿|百亿|十亿|千万|百万|十万|%|％|亿|億|万|萬|千|百"
    r"|million|billion|trillion|bn|mn|tn"
    r"|元|美元|台|辆|人|个|件|家|座|GW|GWh|MW|MWh|kW|kWh|吨|平方米|公里|km|℃|倍|次"
    r"|K(?![A-Za-z])|W(?![A-Za-z])|L(?![A-Za-z]))?",
    re.IGNORECASE,
)
# 只有带单位/小数或至少 3 位数字的 token 才算定量声明；裸“3”多半是编号。
QUANTITY_SUBSTANTIVE = re.compile(r"[%％亿億万萬千百元台辆人个件家座GWkWh吨公里倍次KkLlWw]|\.|[0-9]{3}", re.IGNORECASE)
# 比较级是结论性说法，必须有来源排序或同义表述支撑。
COMPARISON = re.compile(r"仅次于|次高|第二|最高|最低|最大|最小|首位|第一|领先|居首")
COMPARISON_STRONG = re.compile(r"仅次于|次高|第二")
COMPARISON_SUPPORT = {
    "strong": re.compile(r"仅次于|次高|第二|次席|排行第二|排名第二|\bsecond(?:\s+place)?\b|\brunner.up\b", re.IGNORECASE),
    "weak": re.compile(r"最高|最低|最大|最小|首位|第一|领先|居首|top\s*1|top1|第一名|位居首|\btop spot\b|\blargest\b|\bfirst place\b", re.IGNORECASE),
}
# “数据待补充”常被用来掩盖资料里已有的数据；这些指标词用于反查候选来源。
GAP_METRICS = {
    "出货量": re.compile(r"出货(?:量|规模|数量)?|shipment", re.IGNORECASE),
    "零售额": re.compile(r"零售额|销售额|revenue", re.IGNORECASE),
    "零售量": re.compile(r"零售量|销售量|销量", re.IGNORECASE),
    "市场规模": re.compile(r"市场规模|市场总规模|market\s+size", re.IGNORECASE),
    "渗透率": re.compile(r"渗透率|penetration", re.IGNORECASE),
    "增长率": re.compile(r"增长率|增速|同比|year\s+over\s+year|yoy", re.IGNORECASE),
    "份额": re.compile(r"份额|share", re.IGNORECASE),
    "均价": re.compile(r"均价|平均价格|\basp\b", re.IGNORECASE),
}
# 这些词不是数字，但同样是事实断言；来源没有出现时不能凭常识补写。
MECHANISM_TERMS = re.compile(r"芯片|边缘计算|低功耗|自研|专利|算力|云端部署")
THRESHOLD_TERMS = re.compile(r"\d[\d,.]*\s*(?:万元|千元|元|美元|万台)\s*(?:以上|以下)|(?:万元|千元|元|美元)\s*(?:以上|以下)")


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
    factual = re.sub(r'(?:第\s*)?\d+(?:\s*\.\s*\d+)+\s*节|章节\s*\d+(?:\s*\.\s*\d+)+', '', factual)
    factual = ORDER_LABEL.sub("", factual)
    factual = DATE_LABEL.sub("", factual)
    factual = STANDARD_LABEL.sub("", factual)
    factual = RANKING_LABEL.sub("", factual)
    factual = ALPHANUMERIC_LABEL.sub("", factual)
    return SECTION_LABEL.sub("", factual)


def quantitative_tokens(paragraph: str) -> list[str]:
    return [token for token in (match.group(0).strip() for match in QUANTITY.finditer(_factual_text(paragraph)))
            if QUANTITY_SUBSTANTIVE.search(token)]


def _number_parts(tokens: list[str]) -> list[str]:
    numbers = []
    for token in tokens:
        match = re.match(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", token)
        if match:
            numbers.append(match.group(0).replace(",", ""))
    return numbers


def _number_in_text(number: str, text: str) -> bool:
    # 来源常用千分位（1,271亿），token 已去掉逗号；比较前统一去掉分组逗号。
    normalized = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text or "")
    return re.search(r"(?<![\d.])" + re.escape(number) + r"(?![\d])", normalized) is not None


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


def split_sentences(text: str) -> list[str]:
    """按句末标点切句，并把紧跟标点的 [S#] 归到前一句。

    草稿普遍把引用写在句号之后；若按标点直接切分，引用会被算到下一条语句，
    导致“数字不属于被引来源”的假阳性，并把正确引用误判为错引。
    """
    if not text:
        return []
    result, start = [], 0
    for match in re.finditer(r"[。；！？!?]+", text):
        end = match.end()
        while True:
            ref = re.match(r"\s*\[(?:S[0-9]+|F[A-Za-z0-9]+)\]", text[end:])
            if not ref:
                break
            end += ref.end()
        result.append(text[start:end])
        start = end
    if start < len(text):
        result.append(text[start:])
    return result


def _claim_grams(text: str) -> set[str]:
    """两字片段，用于判断句子更像哪一条来源的原文；去数字与标点。"""
    text = re.sub(r"[0-9０-９]+", "", text or "")
    text = re.sub(r"[^一-鿿a-zA-Z]", "", text).lower()
    return {text[i:i + 2] for i in range(len(text) - 1)} or ({text} if text else set())


def _excerpt_coverage(claim: str, excerpt: str) -> float:
    grams = _claim_grams(claim)
    if not grams:
        return 0.0
    return len(grams & _claim_grams(excerpt)) / len(grams)


def _choose_candidate(sentence: str, candidates: list[str], known: dict) -> str | None:
    """多个来源都含同一组数字时，用原文重合度选一个；无明显优势则不猜。"""
    claim = REFERENCE.sub("", _factual_text(sentence))
    scored = sorted(
        ((_excerpt_coverage(claim, known[source_id].get("excerpt", "")), source_id)
         for source_id in candidates),
        reverse=True,
    )
    best_score, best_id = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= 0.5 and best_score - second_score >= 0.2:
        return best_id
    return None


def rebind_citations(body: str, sources) -> dict:
    """按句子把数字声明确定性重挂到匹配来源。

    与 attach_citations 的区别：不仅给“整段没有引用”的段落补编号，还会把
    “有引用但数字不在所引来源里”的句子改挂到真正包含这些数字的来源。

    规则（确定性，不调用模型）：
    - 一句里的全部数字只匹配一个来源 → 直接改为该来源；
    - 匹配多个来源但现有引用已在其列 → 保持不动；
    - 多个来源且现有引用无效 → 原文重合度明显更高者胜出，否则保留原样并记为歧义；
    - 没有任何来源包含全部数字 → 保留原样，交给审核/返工处理。
    """
    known = {
        source.get("source_id"): source
        for source in (sources or [])
        if source.get("source_id") and not source.get("source_deleted")
        and str(source.get("excerpt") or "").strip()
    }
    parts = re.split(r"(\n\s*\n)", body or "")
    rebound, ambiguous, unmatched = 0, [], []
    for index in range(0, len(parts), 2):
        paragraph = parts[index]
        if not paragraph.strip() or re.fullmatch(r"\s*#{1,6} .*", paragraph.strip()):
            continue
        changed = False
        sentences = split_sentences(paragraph)
        for position, sentence in enumerate(sentences):
            tokens = _binding_tokens(sentence)
            if not tokens:
                continue
            candidates = sorted(
                source_id for source_id, source in known.items()
                if all(token_matches_text(token, source.get("excerpt", "")) for token in tokens)
            )
            current = reference_ids(sentence)
            if not candidates:
                unmatched.append({"sentence": " ".join(sentence.split())[:200], "tokens": tokens})
                continue
            if len(candidates) == 1:
                target = candidates[0]
            elif current & set(candidates):
                continue
            else:
                target = _choose_candidate(sentence, candidates, known)
                if target is None:
                    ambiguous.append({
                        "sentence": " ".join(sentence.split())[:200],
                        "tokens": tokens,
                        "candidates": candidates,
                    })
                    continue
            if current == {target}:
                continue
            cleaned = re.sub(r"[ \t]+(?=[。；！？!?])", "", REFERENCE.sub("", sentence))
            trailing = cleaned[len(cleaned.rstrip()):]
            stripped = cleaned.rstrip()
            final = re.search(r"[。；！？!?]+$", stripped)
            if final:
                # 引用放在句末标点之前，审核的句子级绑定检查才能把数字与来源放在同一条语句里。
                stripped = stripped[:final.start()] + f"[{target}]" + final.group(0)
            else:
                stripped += f"[{target}]"
            sentences[position] = stripped + trailing
            changed = True
            rebound += 1
        if changed:
            parts[index] = "".join(sentences)
    return {"body": "".join(parts), "rebound": rebound,
            "ambiguous": ambiguous, "unmatched": unmatched}


def _binding_tokens(paragraph: str) -> list[str]:
    """绑定检查只看市场指标，不把“2 个数据点”或“2.0 时代”当事实。"""
    return [
        token for token in quantitative_tokens(paragraph)
        if re.search(r"[%％亿万千百元台吨倍KkWwLl]|\.|[0-9]{3}", token)
        and not re.fullmatch(r"\d+\s*(?:个|条|件|名|次)", token)
        and not re.fullmatch(r"\d+\.\d", token)
    ]


def _scale_matches(tokens: list[str], known: dict, refs=()) -> dict:
    """找出数值相同但量级不同的来源，用于提示单位换算错误（不自动改数字）。"""
    factors = (10, 100, 1000, 0.1, 0.01, 0.001)
    refs = set(refs)
    result = {}
    for token in tokens:
        token_values = [value for value, kind, _ in parse_number_values(token) if kind == "count"]
        if not token_values:
            continue
        hits = []
        for source_id, source in known.items():
            if source.get("source_deleted"):
                continue
            source_values = [value for value, kind, _ in
                             parse_number_values(source.get("excerpt", "")) if kind == "count"]
            if not source_values:
                continue
            for factor in factors:
                if any(
                    numbers_match((value * factor, "count", None), (other, "count", None))
                    for value in token_values for other in source_values
                ):
                    hits.append({"source": source_id, "factor": factor})
                    break
        if hits:
            unique = {(item["source"], item["factor"]): item for item in hits}
            result[token] = sorted(
                unique.values(),
                key=lambda item: (item["source"] not in refs, item["source"], item["factor"]),
            )[:2]
    return result


def _matched_tokens(tokens: list[str], source_ids, known: dict, claim='') -> dict:
    from services.numeric_claims import claim_token_matches
    return {
        source_id: [token for token in tokens
                    if claim_token_matches(token, claim, known[source_id].get("excerpt", ""))]
        for source_id in source_ids
        if source_id in known and not known[source_id].get("source_deleted")
    }


def _source_supports(tokens: list[str], source_id: str, known: dict, claim='') -> bool:
    from services.numeric_claims import claim_token_matches
    source = known.get(source_id)
    return bool(source) and not source.get("source_deleted") and all(
        claim_token_matches(token, claim, source.get("excerpt", "")) for token in tokens
    )


def _candidate_sources(unbound_numbers: list[str], known: dict, exclude=()) -> list[str]:
    """按“原文写法优先于数值等价”给未绑定数字反查来源，避免图表坐标轴冒充 6K/10K。"""
    exclude = set(exclude)
    scores = {}
    for source_id, source in known.items():
        if source_id in exclude or source.get("source_deleted"):
            continue
        source_text = source.get("excerpt", "")
        compact = re.sub(r"[\s,]", "", source_text).lower()
        score = 0
        for token in unbound_numbers:
            hit = token_matches_text(token, source_text)
            raw_hit = re.sub(r"[\s,]", "", token).lower() in compact
            if hit or raw_hit:
                score += 10 if raw_hit else 1
        scores[source_id] = score
    best = max(scores.values(), default=0)
    return sorted(source_id for source_id, score in scores.items() if score and score == best)


def _classify_binding(unbound_numbers: list[str], candidates: list[str],
                      scale_matches: dict) -> tuple[str, str]:
    """区分三类问题，避免把挂错或单位换算误报成“编造”。"""
    if candidates:
        return "miscitation", "数字不在被引来源，但可在候选来源中找到（引用挂错）"
    if scale_matches:
        first = next(iter(scale_matches.values()))[0]
        return "unit_scale", f"数字与被引来源数值相同但量级相差 {first['factor']} 倍（疑似单位换算）"
    return "unsupported", "数字未在任何来源摘要中找到（可能编造或来源不足）"


def _binding_issues(body: str, known: dict) -> list[dict]:
    """句子级数字绑定：只验证数字能在被引来源摘要中找到，不判断语义。

    未带引用的数字句做段落级兜底：同段有引用、且文档中没有任何被引来源能完整
    支持这些数字时，才报 hard finding；若同一来源已在别处被引用，则不重复要求。
    """
    prose = body
    document_refs = set(REFERENCE.findall(prose))
    issues = []
    for paragraph_no, paragraph in enumerate(re.split(r"\n\s*\n", prose), 1):
        paragraph_refs = sorted(set(REFERENCE.findall(paragraph)))
        for sentence_index, sentence in enumerate(split_sentences(paragraph), 1):
            refs = sorted(set(REFERENCE.findall(sentence)))
            tokens = _binding_tokens(sentence)
            if not tokens:
                continue
            claim_text = " ".join(sentence.split())[:240]
            if not refs:
                if not paragraph_refs or not document_refs:
                    continue
                if any(_source_supports(tokens, source_id, known, sentence) for source_id in document_refs):
                    continue
                matches = _matched_tokens(tokens, paragraph_refs, known, sentence)
                if not matches:
                    continue
                unbound_numbers = [
                    token for token in tokens
                    if not any(token in matched for matched in matches.values())
                ]
                if not unbound_numbers:
                    continue
                candidates = _candidate_sources(unbound_numbers, known, paragraph_refs)
                scale_matches = _scale_matches(unbound_numbers, known, paragraph_refs)
                category, reason = _classify_binding(unbound_numbers, candidates, scale_matches)
                issues.append({
                    "kind": "unbound_number",
                    "severity": "hard",
                    "source_ids": paragraph_refs,
                    "paragraph": paragraph_no,
                    "sentence_index": sentence_index,
                    "sentence": claim_text,
                    "numbers": unbound_numbers,
                    "candidate_sources": candidates,
                    "scale_matches": scale_matches,
                    "category": category,
                    "uncited_sentence": True,
                    "reason": reason,
                })
                continue
            matches = _matched_tokens(tokens, refs, known, sentence)
            if not matches:
                continue
            unbound_numbers = [
                token for token in tokens
                if not any(token in matched for matched in matches.values())
            ]
            if unbound_numbers:
                # 数字不在被引来源时反查未引用来源：命中即可提示“实际出处”，
                # 让返工改引而不是简单删除，避免把正确数据判成编造。
                candidates = _candidate_sources(unbound_numbers, known, refs)
                # 数字完全相同但量级差 10/100/1000 倍时，多半是 billion/亿 等单位换算错误；
                # 检查层只提示倍数；确定性修正由 apply_unit_scale_fixes 完成。
                scale_matches = _scale_matches(unbound_numbers, known, refs)
                category, reason = _classify_binding(unbound_numbers, candidates, scale_matches)
                issues.append({
                    "kind": "unbound_number",
                    "severity": "hard",
                    "source_ids": sorted(set(refs)),
                    "paragraph": paragraph_no,
                    "sentence_index": sentence_index,
                    "sentence": claim_text,
                    "numbers": unbound_numbers,
                    "candidate_sources": candidates,
                    "scale_matches": scale_matches,
                    "category": category,
                    "uncited_sentence": False,
                    "reason": reason,
                })
            for source_id, matched in matches.items():
                if not matched:
                    issues.append({
                        "kind": "unbound_source",
                        "severity": "info",
                        "source_id": source_id,
                        "paragraph": paragraph_no,
                        "sentence_index": sentence_index,
                        "sentence": claim_text,
                        "numbers": tokens,
                        "reason": "该来源摘要不含本句任何数字（成组引用的正常情况）",
                    })
    unique, seen = [], set()
    for issue in issues:
        key = (issue["kind"], issue.get("source_id"), issue.get("paragraph"), issue.get("sentence_index"), issue["sentence"],
               tuple(issue["numbers"]), issue.get("uncited_sentence"))
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


def apply_unit_scale_fixes(body: str, sources) -> dict:
    """把“数字相同、英文量级不同”的机械错误按来源原文确定性改写。

    只在数字完全一致且来源中该数字只对应一个英文量级时替换；其余情况保留原样，
    交给审核与返工。未带引用的句子使用同段引用作为候选，用于修复
    “同段只有一处引用、相邻句漏写引用”的单位错误。
    """
    known = {
        source.get("source_id"): source
        for source in (sources or [])
        if source.get("source_id") and not source.get("source_deleted")
        and str(source.get("excerpt") or "").strip()
    }
    parts = re.split(r"(\n\s*\n)", body or "")
    fixed, skipped = [], []
    for index in range(0, len(parts), 2):
        paragraph = parts[index]
        if not paragraph.strip() or re.fullmatch(r"\s*#{1,6} .*", paragraph.strip()):
            continue
        paragraph_refs = sorted(set(REFERENCE.findall(paragraph)))
        changed = False
        sentences = split_sentences(paragraph)
        for position, sentence in enumerate(sentences):
            refs = sorted(set(REFERENCE.findall(sentence))) or paragraph_refs
            tokens = _binding_tokens(sentence)
            if not refs or not tokens:
                continue
            unbound = [
                token for token in tokens
                if not any(token_matches_text(token, known[source_id].get("excerpt", ""))
                           for source_id in refs if source_id in known)
            ]
            new_sentence = sentence
            for token in unbound:
                conversions = [
                    (source_id, scale_conversion(token, known[source_id].get("excerpt", "")))
                    for source_id in refs if source_id in known
                ]
                conversions = [(source_id, hint) for source_id, hint in conversions if hint]
                if len(conversions) != 1:
                    skipped.append({"token": token,
                                    "reason": "ambiguous" if conversions else "no_conversion"})
                    continue
                source_id, hint = conversions[0]
                if token not in new_sentence:
                    continue
                new_sentence = new_sentence.replace(token, hint["expected"], 1)
                fixed.append({
                    "token": token,
                    "expected": hint["expected"],
                    "source_id": source_id,
                    "factor": hint["factor"],
                })
                changed = True
            sentences[position] = new_sentence
        if changed:
            parts[index] = "".join(sentences)
    return {"body": "".join(parts), "fixed": fixed, "skipped": skipped}


def _comparison_issues(body: str, known: dict) -> list[dict]:
    """比较级必须被被引来源支撑；“仅次于/第二”缺失时按硬问题处理。"""
    issues = []
    for paragraph_no, paragraph in enumerate(re.split(r"\n\s*\n", body), 1):
        for sentence_index, sentence in enumerate(split_sentences(paragraph), 1):
            refs = REFERENCE.findall(sentence)
            tokens = _binding_tokens(sentence)
            comparison_text = _factual_text(sentence)
            if not refs or not tokens or not COMPARISON.search(comparison_text):
                continue
            strong = bool(COMPARISON_STRONG.search(comparison_text))
            support = COMPARISON_SUPPORT["strong" if strong else "weak"]
            if any(support.search(known[source_id].get("excerpt", ""))
                   for source_id in refs if source_id in known):
                continue
            excerpt = " ".join(sentence.split())[:240]
            issues.append({
                "kind": "unverified_comparison",
                "severity": "hard" if strong else "soft",
                "source_ids": sorted(set(refs)),
                "paragraph": paragraph_no,
                "sentence_index": sentence_index,
                "sentence": excerpt,
                "numbers": tokens,
                "reason": "被引来源没有对应的排序或比较表述",
            })
    return issues


def _wording_issues(body: str, known: dict) -> list[dict]:
    """机制词与金额阈值不是数字，但同样必须有来源；来源没有就不能补写。"""
    issues = []
    for paragraph_no, paragraph in enumerate(re.split(r"\n\s*\n", body), 1):
        for sentence_index, sentence in enumerate(split_sentences(paragraph), 1):
            refs = REFERENCE.findall(sentence)
            if not refs:
                continue
            terms = {match.group(0).strip() for match in MECHANISM_TERMS.finditer(sentence)}
            terms |= {match.group(0).strip() for match in THRESHOLD_TERMS.finditer(sentence)}
            if not terms:
                continue
            excerpts = [known[source_id].get("excerpt", "") for source_id in refs if source_id in known]
            if not excerpts:
                continue
            compact = re.sub(r"\s+", "", " ".join(excerpts))
            missing = sorted(term for term in terms if re.sub(r"\s+", "", term) not in compact)
            if not missing:
                continue
            issues.append({
                "kind": "unsupported_wording",
                "severity": "soft",
                "source_ids": sorted(set(refs)),
                "paragraph": paragraph_no,
                "sentence_index": sentence_index,
                "sentence": " ".join(sentence.split())[:240],
                "terms": missing,
                "reason": "被引来源未出现该机制或阈值表述",
            })
    return issues


def _avoidable_gap_issues(body: str, known: dict) -> list[dict]:
    """标了“数据待补充”，但其他来源里已有同指标数字时给出候选。"""
    issues = []
    for paragraph_no, paragraph in enumerate(re.split(r"\n\s*\n", body), 1):
        if "数据待补充" not in paragraph:
            continue
        keywords = [label for label, pattern in GAP_METRICS.items() if pattern.search(paragraph)]
        if not keywords:
            continue
        sentences = split_sentences(paragraph)
        gap_sentence = next(
            (sentence for sentence in sentences if "数据待补充" in sentence),
            paragraph,
        )
        sentence_index = next(
            (index for index, sentence in enumerate(sentences, 1) if "数据待补充" in sentence),
            1,
        )
        primary = {label for label, pattern in GAP_METRICS.items() if pattern.search(gap_sentence)}
        from services.claim_semantics import period_context, source_text
        wanted = period_context(gap_sentence)
        periods = re.findall(
            r"\d{1,2}\s*[-~—至]\s*\d{1,2}\s*月|20\d{2}\s*年(?:H1|上半年)?|第[一二三四1-4]季度",
            gap_sentence,
        )
        scored = []
        for source_id, source in known.items():
            if source.get("source_deleted"):
                continue
            excerpt = source_text(source)
            matched = {label for label, pattern in GAP_METRICS.items() if pattern.search(excerpt)}
            if not matched or (primary and not primary.intersection(matched)):
                continue
            available = period_context(excerpt)
            if wanted['years'] and not set(wanted['years']).intersection(available['years']):
                continue
            if wanted['periods'] and not set(wanted['periods']).intersection(available['periods']):
                continue
            if re.search(r'预测|预计', gap_sentence) and not re.search(r'预测|预计|将|forecast|projected', excerpt, re.I):
                continue
            numbers = quantitative_tokens(excerpt)
            if not numbers:
                continue
            period_hit = any(period.replace(" ", "") in excerpt.replace(" ", "") for period in periods)
            scored.append((
                bool(primary & matched),
                period_hit,
                len(set(keywords) & matched),
                len(numbers),
                source_id,
            ))
        if not scored:
            continue
        scored.sort(reverse=True)
        candidates = [source_id for *_, source_id in scored[:3]]
        issues.append({
            "kind": "avoidable_gap",
            "severity": "soft",
            "paragraph": paragraph_no,
            "sentence_index": sentence_index,
            "sentence": " ".join(paragraph.split())[:240],
            "keywords": keywords,
            "candidate_sources": candidates,
            "reason": "标记了数据待补充，候选来源含同指标数字（需核对统计口径与期间）",
        })
    return issues


def citation_lifecycle(before, after, required, changes=(), original_claims=None):
    """定量删除沿用原规则；定性删除需逐句回执并留给独立审核复核。"""
    from difflib import SequenceMatcher
    def plain(text):
        return re.sub(r'[\W_]+', '', REFERENCE.sub('', text))
    used = reference_ids(after)
    retained, retired, retained_claims = [], [], {}
    for sid in sorted(set(required)):
        claims = [sentence for paragraph in re.split(r'\n\s*\n', before or '')
                  for sentence in split_sentences(paragraph) if sid in reference_ids(sentence)]
        claims = claims or list((original_claims or {}).get(sid, []))
        if sid in used:
            retained.append(sid)
            retained_claims[sid] = claims
            continue
        removed = bool(claims)
        for claim in claims:
            tokens = [t for t in quantitative_tokens(claim) if not re.fullmatch(r'(?:19|20)\d{2}', t)]
            if not tokens or any(token_matches_text(t, after) for t in tokens):
                removed = False
        if removed:
            retired.append({'source_id': sid, 'claims': claims, 'reason': '原定量声明整体删除，未保留其数值'})
        else:
            declared = {c.get('original_claim') for c in changes if isinstance(c, dict)
                        and c.get('source_id') == sid and c.get('action') == 'deleted'}
            qualitative = bool(claims) and all(not quantitative_tokens(claim) for claim in claims)
            remaining = [plain(s) for p in re.split(r'\n\s*\n', after or '') for s in split_sentences(p)]
            absent = all(plain(claim) and not any(plain(claim) in s or
                SequenceMatcher(None, plain(claim), s).ratio() >= .6 for s in remaining) for claim in claims)
            if qualitative and set(claims).issubset(declared) and absent:
                retired.append({'source_id':sid, 'claims':claims, 'reason':'原定性声明逐句声明删除，未保留原句或近似改写，待独立复核',
                                'review_required':True})
            else:
                retained.append(sid)
                retained_claims[sid] = claims
    return {'required_ids': retained, 'retired_ids': [r['source_id'] for r in retired],
            'retired_claims': retired, 'retained_claims':retained_claims}


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
    for paragraph_no, paragraph in enumerate(re.split(r"\n\s*\n", body), 1):
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
    from services.facts import fact_table, merged_range_issues
    from services.claim_semantics import semantic_issues, source_text
    binding_known = {sid: dict(s, excerpt=source_text(s)) for sid, s in known.items()}
    facts = fact_table(known.values())["facts"]
    binding_issues = (
        _binding_issues(body, binding_known)
        + _comparison_issues(body, known)
        + _wording_issues(body, known)
        + _avoidable_gap_issues(body, known)
        + merged_range_issues(body, facts)
        + semantic_issues(body, list(known.values()))
    )
    return {
        "valid": not errors,
        "citations": records,
        "errors": list(dict.fromkeys(errors)),
        "issues": issues,
        "binding_issues": binding_issues,
    }


def evidence_context(research_data: dict, sources=None, budget=12000, query: str | None = None,
                    required_ids=None) -> str:
    if budget < 256:
        raise ValueError("证据摘要预算至少256字符")
    sources = normalize_evidence("preview", research_data) if sources is None else sources
    if required_ids:
        context = required_evidence_context(query or '', sources, required_ids, budget)
        context['steps'] = [str(key)[:64] for key in list(research_data)[:20]]
        if len(json.dumps(context, ensure_ascii=False)) > 64000:
            raise ValueError('EVIDENCE_CONTEXT_OVERFLOW：完整必需证据及步骤超过上下文硬上限')
        return json.dumps(context, ensure_ascii=False)
    context = {"untrusted": True, "steps": list(research_data)[:20], "sources": []}
    if not sources:
        context["notice"] = "（暂无研究数据）" if not research_data else "无可追溯来源，数据待补充"
    # 按项预算，每个候选都是完整JSON，永不截断序列化后的字符串。
    context["steps"] = [str(key)[:64] for key in context["steps"]]
    while len(json.dumps(context, ensure_ascii=False)) > budget:
        context["steps"].pop()
    if query:
        ranked = rank_sources(query, sources, budget=max(256, budget - 512), required_ids=required_ids)
        for source in ranked["selected"]:
            meta = ranked["meta"][source["source_id"]]
            context["sources"].append(evidence_item(source, meta, meta.get("excerpt_limit", 600)))
        if not context["sources"]:
            context.setdefault("notice", "无可追溯来源，数据待补充")
        if budget >= 1024:
            context["selection"] = {
                "selected": [source["source_id"] for source in ranked["selected"]],
                "dropped": ranked["dropped"],
            }
        while len(json.dumps(context, ensure_ascii=False)) > budget and context["sources"]:
            removed = context["sources"].pop()
            if "selection" in context:
                context["selection"]["selected"] = [
                    source_id for source_id in context["selection"]["selected"]
                    if source_id != removed["source_id"]
                ]
                context["selection"]["dropped"].append(
                    {"source_id": removed["source_id"], "reason": "budget"}
                )
        if len(json.dumps(context, ensure_ascii=False)) > budget and "selection" in context:
            context.pop("selection")
        return json.dumps(context, ensure_ascii=False)
    for source in sources:
        if source.get("source_deleted"):
            continue
        item = {k: source.get(k) for k in ("source_id", "kind", "title", "url", "document_id", "chunk_id", "page", "score", "source_deleted", "excerpt", "retrieved_at")}
        item["title"] = item["title"][:120]
        from services.evidence_excerpt import select_excerpt
        item["excerpt"] = select_excerpt(item["excerpt"], 600)
        candidate = context | {"sources": context["sources"] + [item]}
        if len(json.dumps(candidate, ensure_ascii=False)) > budget:
            break
        context = candidate
    return json.dumps(context, ensure_ascii=False)
