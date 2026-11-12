"""结构化事实表：从证据摘要抽取可引用事实，供写作与后置规则使用。

只做确定性抽取，不判断事实真伪。每个数字保留来源、指标、数值、单位、年份与
实绩/预测标签；同一来源的多行预测分行保存，不合并成区间。
"""
import re

from services.numbers import numbers_match, parse_number_values, split_number_token

METRIC_PATTERNS = {
    "市场规模": re.compile(
        r"市场规模|市场总规模|市場規模|市場總規模|产业规模|產業規模|行业规模|行業規模"
        r"|market\s+size|market\s+value|\bmarket\b", re.I),
    "出货量": re.compile(r"出货量|出货规模|出貨量|出貨規模|shipments?|出货|出貨", re.I),
    "零售额": re.compile(r"零售额|销售额|零售額|銷售額|revenue", re.I),
    "销量": re.compile(r"销量|销售量|零售量|銷量|銷售量|內銷|内销", re.I),
    "保有量": re.compile(r"保有量|保有|ownership|installed\s+base", re.I),
    "渗透率": re.compile(r"渗透率|滲透率|penetration", re.I),
    "增长率": re.compile(
        r"增长率|增速|同比|成長率|增長率|year[- ]over[- ]year|yoy|cagr|复合年增长率|複合年增長率", re.I),
    "份额": re.compile(r"份额|市占率|市佔率|占有率|佔有率|share", re.I),
    "均价": re.compile(r"均价|平均价格|均價|平均價格|\basp\b", re.I),
    "装机量": re.compile(r"装机量|装机|裝機量|裝機", re.I),
    "价格": re.compile(r"价格|售价|價格|售價|price", re.I),
}
FORECAST = re.compile(
    r"预计|预测|預測|將|将|forecast|projected|expected|outlook|目标|目標|计划|計劃"
    r"|cagr|复合年增长率|複合年增長率",
    re.I,
)
_AFTER_PREPOSITION = re.compile(r"\b(?:in|by|at|of|for|since)\b|(?:在|於|于|到|至|截至)", re.I)
ACTUAL = re.compile(
    r"达到|達到|已达|已達|突破|为|為|实际|實際|实现|實現|recorded|reported|totaled|\bwas\b|\bwere\b|\bis\b", re.I)
YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
STANDARD = re.compile(r"(?i)(?:GB|IEC|ISO|EN|UL|CSA)\s*/?\s*T?\s*\d+(?:\.\d+)*(?:[-—:\s]\d{1,4})*")
_DATELINE = re.compile(
    r"(?:^|\n)\s*[A-Z][A-Za-z.\- ]{1,40},\s*"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2},\s*"
    r"((?:19|20)\d{2})(?=\s*[—–-])"
)
_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_RANGE = re.compile(
    rf"(?<!\d)(?P<start>{_NUMBER})\s*"
    r"(?P<unit>%|％|万亿|千亿|百亿|十亿|千万|百万|十万|亿|億|万|萬|千|百"
    r"|million|billion|trillion|bn|mn|tn)?"
    r"\s*(?:至|到|[-–—~～])\s*"
    rf"(?P<end>{_NUMBER})\s*"
    r"(?P<end_unit>%|％|万亿|千亿|百亿|十亿|千万|百万|十万|亿|億|万|萬|千|百"
    r"|million|billion|trillion|bn|mn|tn)?",
    re.IGNORECASE,
)
ROLE_LABEL = {"actual": "实绩", "forecast": "预测", "unknown": "未标注"}
RATE_METRICS = {"增长率", "渗透率", "份额"}


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。；！？!?])|(?<=[.!?])\s+|\n+", text or "")
    return [re.sub(r"^\s*#{1,6}\s*", "", part).strip() for part in parts if part.strip()]


def _is_thousands_separator(sentence: str, index: int) -> bool:
    """英文千分位逗号（542,000）不是分句边界。"""
    return (
        sentence[index] == ','
        and index > 0
        and sentence[index - 1].isdigit()
        and sentence[index + 1:index + 4].isdigit()
        and len(sentence[index + 1:index + 4]) == 3
        and (index + 4 == len(sentence) or not sentence[index + 4].isdigit())
    )


def _clause_span(sentence: str, position: int) -> tuple[int, int]:
    start = 0
    for match in re.finditer(r"[，,；;•·|\n\t]", sentence):
        if _is_thousands_separator(sentence, match.start()):
            continue
        if position < match.start():
            return start, match.start()
        start = match.end()
    return start, len(sentence)


def _clause_at(sentence: str, position: int) -> str:
    start, end = _clause_span(sentence, position)
    return sentence[start:end]


def _year_positions(sentence: str) -> list[tuple[int, int]]:
    standard_spans = [(match.start(), match.end()) for match in STANDARD.finditer(sentence)]
    dateline_spans = [(match.start(1), match.end(1)) for match in _DATELINE.finditer(sentence)]
    years = []
    for match in YEAR.finditer(sentence):
        if any(start <= match.start() < end for start, end in standard_spans):
            continue
        # 新闻电头日期是发布时间，不是数据期间。
        if any(start <= match.start() < end for start, end in dateline_spans):
            continue
        years.append((match.start(), int(match.group(1))))
    return years


def _nearest_year(sentence: str, position: int) -> int | None:
    years = _year_positions(sentence)
    if not years:
        return None
    start, end = _clause_span(sentence, position)
    in_clause = [(year_pos, year) for year_pos, year in years if start <= year_pos < end]
    if in_clause:
        before = [(year_pos, year) for year_pos, year in in_clause if year_pos < position]
        after = [(year_pos, year) for year_pos, year in in_clause if year_pos >= position]
        if after:
            year_pos, year = min(after, key=lambda item: item[0] - position)
            # “from X in 2026 to Y by 2035”：数字后面若紧跟介词+年份，优先取后置年份。
            if _AFTER_PREPOSITION.search(sentence[position:year_pos]):
                return year
        if before:
            return max(before, key=lambda item: item[0])[1]
        return min(after, key=lambda item: item[0] - position)[1] if after else None
    # 分句没有年份时优先继承前一个分句的年份；只有前面没有年份才看后面，
    # 避免把“同比增长12.7%”错挂到后一句的“预计到2026年”。
    before = [(year_pos, year) for year_pos, year in years if year_pos < start]
    if before:
        return max(before, key=lambda item: item[0])[1]
    after = [(year_pos, year) for year_pos, year in years if year_pos >= end]
    return min(after, key=lambda item: item[0] - position)[1] if after else None


def _nearest_metric(sentence: str, position: int, kind: str = "count") -> str:
    matches = [
        (abs(match.start() - position), label)
        for label, pattern in METRIC_PATTERNS.items()
        for match in pattern.finditer(sentence)
    ]
    if not matches:
        return "未分类"
    preferred = [
        item for item in matches
        if (item[1] in RATE_METRICS) == (kind == "percent")
    ] or matches
    return min(preferred, key=lambda item: (item[0], item[1]))[1]


def _role(clause: str, sentence: str = "") -> str:
    if FORECAST.search(clause):
        return "forecast"
    if ACTUAL.search(clause):
        return "actual"
    if sentence:
        marker = FORECAST.search(sentence)
        if marker:
            # 分句位于第一个预测标记之前时，不继承后面的预测标签；
            # 避免把“2023年已达…”或“同比增长12.7%”一起判成预测。
            clause_at = sentence.find(clause)
            if 0 <= clause_at < marker.start():
                return "actual" if ACTUAL.search(sentence[:marker.start()]) else "unknown"
            return "forecast"
        if ACTUAL.search(sentence):
            return "actual"
    return "unknown"


def _currency(clause: str, suffix: str) -> str | None:
    text = f"{clause} {suffix}".lower()
    for code, pattern in (
        ("USD", r"美元|usd|\$|dollar"),
        ("EUR", r"欧元|eur"),
        ("JPY", r"日元|jpy"),
        ("HKD", r"港元|hkd"),
        ("CNY", r"人民币|cny|rmb|元"),
    ):
        if re.search(pattern, text):
            return code
    return None


def _unit_kind(unit: str, currency: str | None) -> str:
    if "%" in unit or "％" in unit:
        return "percent"
    if currency:
        return "currency"
    if any(unit.endswith(marker) or marker in unit for marker in ("台", "辆", "个", "件", "家", "座", "人", "名", "吨")):
        return "count"
    return "number"


def _is_year_token(token: str) -> bool:
    return re.fullmatch(r"(?:19|20)\d{2}", token.strip()) is not None


_CODE_CONTEXT = re.compile(r"报告编号|报告号|编号|報告編號|報告號|report\s*(?:id|no)|isbn|doi|页码|頁碼", re.I)


def _is_code_token(token: str, clause: str) -> bool:
    """无单位的纯整数在编号/报告号语境中不是事实。"""
    if not re.fullmatch(r"\d{3,}", token.strip()):
        return False
    return bool(_CODE_CONTEXT.search(clause))


def _find_token(sentence: str, token: str, start: int = 0) -> int:
    """按空白容错定位 token（QUANTITY 可能合并“10.5”与“%”）。"""
    pattern = r"\s*".join(re.escape(part) for part in re.split(r"\s+", token.strip()))
    match = re.compile(pattern).search(sentence, start)
    return match.start() if match else -1


_CURRENCY_UNIT = {"USD": "美元", "CNY": "元", "EUR": "欧元", "JPY": "日元", "HKD": "港元"}
_COUNT_SUFFIXES = ("台", "辆", "个", "件", "家", "座", "人", "名", "吨")


def _count_suffix_after(sentence: str, position: int, token: str) -> str:
    tail = sentence[position + len(token):position + len(token) + 2]
    return next((marker for marker in _COUNT_SUFFIXES if tail.startswith(marker)), "")


def extract_facts(sources) -> list[dict]:
    """从来源摘要抽取事实行；同来源多条预测分行保留。"""
    from services.citations import quantitative_tokens

    facts = []
    for source in sources or []:
        if not isinstance(source, dict) or source.get("source_deleted"):
            continue
        source_id = source.get("source_id")
        excerpt = str(source.get("excerpt") or "")
        if not source_id or not excerpt.strip():
            continue
        from services.claim_semantics import period_context, source_text, object_context
        source_period = period_context(_DATELINE.sub(" ", source_text(source)))
        title_period = period_context(str(source.get('title') or ''))
        regions = [label for label in ('中国', '全球', '北美', '欧洲') if label in source_text(source)]
        for sentence in _sentences(excerpt):
            cursor = 0
            for token in quantitative_tokens(sentence):
                if _is_year_token(token):
                    continue
                position = _find_token(sentence, token, cursor)
                if position < 0:
                    position = _find_token(sentence, token)
                if position < 0:
                    continue
                cursor = position + len(token)
                parts = split_number_token(token)
                values = parse_number_values(token)
                if not parts or not values:
                    continue
                number, cn_unit, suffix = parts
                normalized, kind, _ = values[0]
                clause = _clause_at(sentence, position)
                if _is_code_token(token, clause):
                    continue
                currency = None if kind == "percent" else _currency(clause, suffix)
                unit = f"{cn_unit or ''}{suffix}".strip()
                if currency and cn_unit and not suffix:
                    unit += _CURRENCY_UNIT.get(currency, "")
                elif not suffix and not currency:
                    unit += _count_suffix_after(sentence, position, token)
                local_period = period_context(sentence)
                local_regions = [label for label, pattern in (
                    ('中国', r'中国|国内|我国'), ('全球', r'全球|global|worldwide'),
                    ('北美', r'北美|North America'), ('欧洲', r'欧洲|Europe')) if re.search(pattern, sentence, re.I)]
                fact_regions = local_regions or regions
                objects = object_context(clause) or object_context(sentence) or object_context(str(source.get('title') or ''))
                year = _nearest_year(sentence, position)
                if year is None:
                    if len(title_period['years']) == 1:
                        year = title_period['years'][0]
                    elif len(source_period['years']) == 1:
                        year = source_period['years'][0]
                period = local_period['periods']
                if not period and len(title_period['periods']) == 1 and (
                        not title_period['years'] or title_period['years'] == [year]):
                    period = title_period['periods']
                if not period and len(source_period['periods']) == 1 and (
                        not source_period['years'] or source_period['years'] == [year]):
                    period = source_period['periods']
                tail = sentence[position + len(token):]
                measure = ('percentage_point' if re.match(r'个?百分点', tail) else
                           'yoy_rate' if kind == 'percent' and '同比' in clause else
                           'qoq_rate' if kind == 'percent' and '环比' in clause else kind)
                if measure == 'percentage_point':
                    unit = '个百分点'
                facts.append({
                    "source_id": source_id,
                    "metric": _nearest_metric(sentence, position, kind),
                    "value": number,
                    "unit": unit,
                    "normalized": normalized,
                    "kind": kind,
                    "unit_kind": _unit_kind(unit, currency),
                    "currency": currency,
                    "year": year,
                    "period": period[0] if len(period) == 1 else None,
                    "geography": fact_regions[0] if len(fact_regions) == 1 else None,
                    "measure": measure,
                    "statistical_object": objects[0] if len(objects) == 1 else None,
                    "role": _role(clause, sentence),
                    "text": " ".join(sentence.split())[:200],
                })
    unique, seen = [], set()
    for fact in sorted(facts, key=lambda item: (int(item["source_id"][1:]), item["year"] or 0,
                                                item["metric"], item["value"])):
        key = (fact["source_id"], fact["metric"], fact["value"], fact["unit"],
               fact["year"], fact["role"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(fact)
    return unique


def _conflicts(facts: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for fact in facts:
        if fact["year"] is None or fact["unit_kind"] == "number":
            continue
        groups.setdefault((fact["metric"], fact["year"], fact["unit_kind"],
                           fact.get('period'), fact.get('geography'), fact.get('measure'),
                           fact.get('statistical_object')), []).append(fact)
    conflicts = []
    for (metric, year, unit_kind, period, geography, measure, statistical_object), items in groups.items():
        distinct = []
        for item in items:
            if not any(
                numbers_match((item["normalized"], unit_kind, None),
                              (other["normalized"], unit_kind, None))
                for other in distinct
            ):
                distinct.append(item)
        if len(distinct) < 2:
            continue
        conflicts.append({
            "metric": metric,
            "year": year,
            "unit_kind": unit_kind,
            "period": period, "geography": geography, "measure": measure,
            "statistical_object": statistical_object,
            "facts": distinct[:5],
            "source_ids": sorted({item["source_id"] for item in distinct}),
            "reason": "同一指标和年份存在多个数值，写作时必须并列并注明来源，不得合并成区间",
        })
    conflicts.sort(key=lambda item: (item["year"] or 0, item["metric"]))
    return conflicts


def fact_table(sources) -> dict:
    facts = extract_facts(sources)
    return {"facts": facts, "conflicts": _conflicts(facts)}


def fact_block(table: dict, budget: int = 2400) -> str:
    """把事实表压成可注入 Writer 提示的文本；超预算时从后往前删行。"""
    facts = table.get("facts") or []
    conflicts = table.get("conflicts") or []
    if not facts:
        return ""
    header = ["结构化事实表（启发式有界索引，可能裁剪，未列行须回查同组完整来源；未知字段不得补造，原文为准；"
              "不得把不同行合并成区间，预测仍须保留预测标签）："]
    rows = [
        f"- {fact['source_id']} | {fact['metric']} | {fact['value']} {fact['unit']} | "
        f"{fact['year'] or '年份未注明'}/{fact.get('period') or '期间未注明'} | "
        f"{fact.get('geography') or '地区未注明'}/{fact.get('measure') or '口径未注明'} | "
        f"{fact.get('statistical_object') or '统计对象未注明'} | "
        f"{ROLE_LABEL.get(fact['role'], '未标注')} | "
        f"{fact['text']}"
        for fact in facts
    ]
    footer = ["英文量级词（billion/Bn/million/Mn）必须先换算为中文单位再写；预测保留“预计/预测”标签。"]
    if conflicts:
        footer.insert(0, "指标冲突（必须并列并注明来源，不得合并成区间）：")
        for conflict in conflicts[:5]:
            values = "、".join(
                f"{fact['source_id']}={fact['value']}{fact['unit']}" for fact in conflict["facts"]
            )
            footer.insert(-1 if len(footer) > 1 else 0,
                          f"- {conflict['metric']}/{conflict['year']}：{values}")
    while rows:
        block = "\n".join(header + rows + footer)
        if len(block) <= budget:
            return block
        rows.pop()
    return "\n".join(header + footer)[:budget]


def _range_value(number: str, unit: str) -> tuple[float, str] | None:
    values = parse_number_values(f"{number}{unit}")
    return (values[0][0], values[0][1]) if values else None


def _bound_in_text(bound: str, text: str) -> bool:
    return re.search(r'(?<![\d.])' + re.escape(bound) + r'(?![\d])', text or '') is not None


def _fact_matches(fact: dict, normalized: float, kind: str) -> bool:
    if kind == "percent":
        return fact["unit_kind"] == "percent" and numbers_match(
            (fact["normalized"], "percent", None), (normalized, "percent", None))
    return fact["unit_kind"] != "percent" and numbers_match(
        (fact["normalized"], "count", None), (normalized, "count", None))


def _sentence_index(paragraph: str, position: int) -> int:
    count = 1
    for match in re.finditer(r"[。；！？!?]+|(?<=[.!?])\s+", paragraph):
        if match.end() <= position:
            count += 1
        else:
            break
    return count


def merged_range_issues(body: str, facts: list[dict]) -> list[dict]:
    """检测“把两个独立事实合并成区间”的写法。"""
    issues = []
    for paragraph_no, paragraph in enumerate(re.split(r"\n\s*\n", body or ""), 1):
        for match in _RANGE.finditer(paragraph):
            start, end = match.group("start"), match.group("end")
            if _is_year_token(start) or _is_year_token(end):
                continue
            unit = match.group("unit") or match.group("end_unit") or ""
            if not unit:
                continue
            start_value = _range_value(start, unit)
            end_value = _range_value(end, unit)
            if not start_value or not end_value:
                continue
            cited = set(re.findall(r"\[(S\d+)\]", paragraph))
            if any(
                fact["source_id"] in cited
                and _bound_in_text(start, fact.get("text", ""))
                and _bound_in_text(end, fact.get("text", ""))
                for fact in facts
            ):
                # 被引来源同一原句同时包含上下界时，这是来源自身的区间，不是合并两个事实。
                continue
            start_facts = [fact for fact in facts
                           if _fact_matches(fact, start_value[0], start_value[1])]
            end_facts = [fact for fact in facts
                         if _fact_matches(fact, end_value[0], end_value[1])]
            if not start_facts or not end_facts:
                continue
            if any(
                left["source_id"] == right["source_id"]
                and left["text"] == right["text"]
                and left["metric"] == right["metric"]
                for left in start_facts for right in end_facts
            ):
                continue
            sources = sorted({fact["source_id"] for fact in start_facts + end_facts})
            issues.append({
                "kind": "merged_fact_range",
                "severity": "hard",
                "paragraph": paragraph_no,
                "sentence_index": _sentence_index(paragraph, match.start()),
                "sentence": " ".join(match.group(0).split()),
                "numbers": [f"{start}{unit}", f"{end}{unit}"],
                "source_ids": sources,
                "candidate_sources": sources,
                "reason": "两个独立事实被合并成区间；必须分别引用并说明统计口径",
            })
    unique, seen = [], set()
    for issue in issues:
        key = (issue["paragraph"], tuple(issue["numbers"]), tuple(issue["source_ids"]))
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique
