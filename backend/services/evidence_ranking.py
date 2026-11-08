"""证据选择：按章节相关性、时效、权威性、信息密度与新颖性排序。

硬约束优先于打分：最新实绩、冲突口径和唯一指标先占位，再按边际价值填充。
排序结果只用于写作/审核上下文，不删除 evidence 本身。
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import date
from urllib.parse import urlparse

from services.numbers import parse_number_values

_OFFICIAL_HOSTS = (
    "gartner.com", "idc.com", "canalys.com", "trendforce.com",
    "counterpointresearch.com", "omdia.tech.informa.com", "npd.com",
)
_MEDIA_HOSTS = (
    "21jingji.com", "futunn.com", "zhitongcaijing.com", "jiemian.com",
    "tmtpost.com", "sina.com.cn", "ithome.com", "finance.sina.com.cn",
)
_AGGREGATOR_HOSTS = (
    "199it.com", "sgpjbg.com", "deepreport.cn", "xcc.com", "mordorintelligence.com",
)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_QUARTERS = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}

_ACTUAL = re.compile(
    r"实际|已|出货量(?:为|达)|总计|初步统计|"
    r"totaled|reported|preliminary|down\s+[\d.]+\s*%.*?year over year|"
    r"up\s+[\d.]+\s*%.*?year over year",
    re.IGNORECASE,
)
_FORECAST = re.compile(
    r"预计|预测|展望|forecast|expected|outlook|projected|\bwill\b",
    re.IGNORECASE,
)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _date_from_text(text: str, fallback_year: int | None = None) -> date | None:
    patterns = (
        (re.compile(r"(?<!\d)(20\d{2})-(\d{1,2})-(\d{1,2})(?!\d)"), "ymd"),
        (re.compile(r"(?<!\d)(20\d{2})/(\d{1,2})/(\d{1,2})(?!\d)"), "ymd"),
        (re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日"), "ymd"),
        (re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(20\d{2})"), "dmy_en"),
        (re.compile(r"([A-Za-z]{3,9})\s+(\d{1,2}),\s*(20\d{2})"), "mdy_en"),
        (re.compile(r"/(20\d{2})(\d{2})(\d{2})/"), "ymd_compact"),
        (re.compile(r"(20\d{2})\s*年(\d{1,2})月"), "ym"),
        (re.compile(r"(\d{1,2})月(\d{1,2})日"), "md"),
    )
    for pattern, kind in patterns:
        for match in pattern.finditer(text or ""):
            groups = match.groups()
            if kind == "ymd":
                result = _safe_date(int(groups[0]), int(groups[1]), int(groups[2]))
            elif kind == "ymd_compact":
                result = _safe_date(int(groups[0]), int(groups[1]), int(groups[2]))
            elif kind == "ym":
                result = _safe_date(int(groups[0]), int(groups[1]), 1)
            elif kind == "md":
                if fallback_year is None:
                    continue
                result = _safe_date(fallback_year, int(groups[0]), int(groups[1]))
            elif kind == "dmy_en":
                month = _MONTHS.get(str(groups[1])[:3].lower())
                result = _safe_date(int(groups[2]), month, int(groups[0])) if month else None
            else:
                month = _MONTHS.get(str(groups[0])[:3].lower())
                result = _safe_date(int(groups[2]), month, int(groups[1])) if month else None
            if result:
                return result
    years = [int(match.group(1)) for match in re.finditer(r"(?<!\d)(20\d{2})(?!\d)", text or "")]
    if years:
        year = max(years)
        if fallback_year and year > fallback_year:
            year = fallback_year
        return _safe_date(year, 1, 1)
    return None


def parse_published_at(source: dict, fallback_year: int | None = None) -> date | None:
    explicit = source.get("published_at") or source.get("published") or source.get("date")
    if isinstance(explicit, str):
        parsed = _date_from_text(explicit, fallback_year)
        if parsed:
            return parsed
    text = " ".join(str(source.get(key) or "") for key in ("title", "excerpt", "url"))
    return _date_from_text(text, fallback_year)


def classify_role(source: dict) -> str:
    text = f"{source.get('title', '')} {source.get('excerpt', '')}"
    actual = len(_ACTUAL.findall(text))
    forecast = len(_FORECAST.findall(text))
    if actual > forecast:
        return "actual"
    if forecast > actual:
        return "forecast"
    return "context"


def authority_score(source: dict) -> float:
    if source.get("kind") == "document":
        return 0.9
    host = (urlparse(source.get("url") or "").hostname or "").lower()
    if any(host == item or host.endswith("." + item) for item in _OFFICIAL_HOSTS):
        return 1.0
    if any(host == item or host.endswith("." + item) for item in _MEDIA_HOSTS):
        return 0.8
    if any(host == item or host.endswith("." + item) for item in _AGGREGATOR_HOSTS):
        return 0.6
    return 0.4


def _substantive_tokens(source: dict) -> list[str]:
    from services.citations import quantitative_tokens

    text = f"{source.get('title', '')} {source.get('excerpt', '')}"
    kept = []
    for token in quantitative_tokens(text):
        number = re.match(r"\d+(?:\.\d+)?", token)
        if not number:
            continue
        raw = number.group(0)
        if re.fullmatch(r"(?:19|20)\d{2}", raw):
            continue
        # 排除“199IT”这类嵌在字母/数字串里的编号，它们不是指标。
        if not re.search(rf"(?i)(?<![a-z0-9.]){re.escape(raw)}(?![a-z0-9])", text):
            continue
        kept.append(token)
    return kept


def density_score(source: dict, role: str) -> float:
    tokens = _substantive_tokens(source)
    if not tokens:
        return 0.1
    return min(1.0, len(tokens) / 10.0) + (0.2 if role == "actual" else 0.0)


def _text_grams(text: str) -> set[str]:
    lowered = (text or "").lower()
    grams = set(re.findall(r"[一-鿿]{1,2}", lowered))
    grams.update(re.findall(r"[a-z0-9]{2,}", lowered))
    return grams


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


_STOP_TERMS = {
    "研究", "主题", "子任务", "基于", "调研", "数据", "撰写", "报告",
    "分析", "总结", "进行", "相关", "主要", "以及", "并且", "第一",
    "第二", "第三", "第四", "章节", "的", "和", "与", "为", "在",
    "对", "中", "年", "月", "日",
}


_CJK_NORMALIZE = str.maketrans({
    "貨": "货", "儲": "储", "價": "价", "預": "预", "測": "测", "電": "电",
    "腦": "脑", "機": "机", "產": "产", "業": "业", "營": "营", "額": "额",
    "數": "数", "據": "据", "場": "场", "際": "际", "體": "体", "網": "网",
    "絡": "络", "應": "应", "與": "与", "為": "为", "這": "这", "個": "个",
    "們": "们", "時": "时", "間": "间", "開": "开", "關": "关", "於": "于",
    "後": "后", "進": "进", "還": "还", "從": "从", "對": "对", "總": "总",
    "億": "亿", "萬": "万", "點": "点", "長": "长", "問": "问", "題": "题",
    "趨": "趋", "勢": "势", "競": "竞", "爭": "争", "滲": "渗", "過": "过",
    "來": "来", "說": "说", "將": "将", "會": "会", "發": "发", "現": "现",
    "實": "实", "鏈": "链", "戶": "户", "購": "购", "買": "买", "費": "费",
    "規": "规", "顯": "显",
})


def _terms(text: str) -> set[str]:
    text = (text or "").translate(_CJK_NORMALIZE)
    terms = set()
    for run in re.findall(r"[一-鿿]+", text):
        for size in (2, 3):
            for index in range(len(run) - size + 1):
                term = run[index:index + size]
                if term not in _STOP_TERMS:
                    terms.add(term)
    terms.update(re.findall(r"[a-z0-9]{2,}", text.lower()))
    return terms


_FOCUS_TRIGGERS = {
    "user": ("用户", "洞察", "消费"),
    "tech": ("技术", "趋势"),
    "competition": ("竞争", "格局"),
    "channel": ("渠道", "二手"),
    "market": ("市场", "宏观", "出货", "规模"),
}
_FOCUS_TERMS = {
    "user": ("购买", "二手", "consumer", "consumers", "buyer", "buyers", "demand", "purchase", "upgrade", "behavior", "refurbished", "survey", "respond", "finds", "study"),
    "tech": ("技术", "趋势", "AI", "芯片", "智能", "算力", "渗透", "tech", "chip", "hardware", "penetration"),
    "competition": ("竞争", "格局", "份额", "品牌", "厂商", "集中度", "share", "vendor", "concentration"),
    "channel": ("渠道", "电商", "零售", "二手", "refurbished", "second-hand", "retail", "channel"),
    "market": ("市场", "宏观", "总量", "出货", "规模", "全球", "中国", "market", "shipment", "volume"),
}


def _focus_terms(key: str) -> set[str]:
    return _terms(" ".join(_FOCUS_TERMS[key]))


def _focus_sources(query: str, sources: list[dict], meta: dict) -> list[dict]:
    chosen = []
    for key, triggers in _FOCUS_TRIGGERS.items():
        if not any(trigger in query for trigger in triggers):
            continue
        terms = _focus_terms(key)
        ranked = sorted(
            sources,
            key=lambda source: (
                len(terms & _terms(f"{source.get('title', '')} {source.get('excerpt', '')}")),
                meta[source["source_id"]]["score"],
            ),
            reverse=True,
        )
        for source in ranked[:2]:
            if source not in chosen:
                meta[source["source_id"]]["focus"] = True
                chosen.append(source)
    return chosen


_QUERY_EXPANSIONS = {
    "技术": ("AI", "芯片", "智能", "硬件", "算力", "chip", "hardware", "compute"),
    "趋势": ("渗透", "增长", "演进", "预测", "growth", "penetration", "forecast", "outlook"),
    "用户": ("消费者", "消费", "需求", "购买", "换机", "consumer", "buyer", "demand", "purchase", "upgrade"),
    "洞察": ("需求", "消费", "用户", "行为", "demand", "behavior"),
    "竞争": ("份额", "品牌", "厂商", "集中度", "share", "vendor", "concentration"),
    "市场": ("出货", "规模", "价格", "ASP", "market", "shipment", "price", "revenue"),
    "宏观": ("全球", "中国", "出货", "规模", "走势", "global", "china", "shipment"),
    "总量": ("出货", "规模", "shipment", "volume"),
    "结构": ("细分", "份额", "structure", "segment"),
    "渠道": ("电商", "二手", "零售", "平台", "channel", "retail", "refurbished", "second-hand"),
    "策略": ("建议", "布局", "供应链", "投资", "strategy", "recommendation", "supply chain"),
    "建议": ("布局", "供应链", "投资", "矩阵", "recommendation", "supply chain"),
    "行业": ("厂商", "供应链", "产业", "industry", "vendor", "supply chain"),
    "电脑": ("PC", "个人电脑", "笔记本", "laptop", "notebook"),
}


def _relevance_scores(query: str, sources: list[dict]) -> dict[str, float]:
    if not query:
        return {source["source_id"]: 0.5 for source in sources}
    query_terms = _terms(query)
    for key, extras in _QUERY_EXPANSIONS.items():
        if key in query:
            query_terms.update(_terms(" ".join(extras)))
    if not query_terms:
        return {source["source_id"]: 0.5 for source in sources}
    source_terms = {
        source["source_id"]: _terms(
            f"{source.get('title', '')} {source.get('title', '')} {source.get('excerpt', '')}"
        )
        for source in sources
    }
    df = defaultdict(int)
    for terms in source_terms.values():
        for term in terms:
            df[term] += 1
    # 只保留至少在一个来源中出现的查询词，避免长句组合词稀释相关性。
    matched_terms = {term for term in query_terms if df.get(term)}
    if not matched_terms:
        return {source["source_id"]: 0.0 for source in sources}
    idf = {
        term: math.log((len(sources) + 1) / (df[term] + 1)) + 1.0
        for term in matched_terms
    }
    total = sum(idf.values()) or 1.0
    return {
        source_id: min(1.0, sum(idf[term] for term in matched_terms & terms) / total)
        for source_id, terms in source_terms.items()
    }


def relevance_score(query: str, source: dict) -> float:
    return _relevance_scores(query, [source]).get(source.get("source_id"), 0.5)


def freshness_score(published: date | None, today: date) -> float:
    if published is None:
        return 0.5
    days = (today - published).days
    if days < 0:
        return 0.5
    if days <= 30:
        return 1.0
    if days <= 90:
        return 0.8
    if days <= 180:
        return 0.6
    if days <= 365:
        return 0.4
    return 0.2


def _period(text: str) -> str | None:
    year = re.search(r"(20\d{2})年?", text)
    quarter = re.search(r"(20\d{2})\s*[Qq]([1-4])", text)
    if quarter:
        return f"{quarter.group(1)}Q{quarter.group(2)}"
    trailing_quarter = re.search(r"[Qq]([1-4])\s*(20\d{2})", text)
    if trailing_quarter:
        return f"{trailing_quarter.group(2)}Q{trailing_quarter.group(1)}"
    chinese = re.search(r"(20\d{2})年?(?:第)?([一二三四1-4])季度", text)
    if chinese:
        return f"{chinese.group(1)}Q{_QUARTERS[chinese.group(2)]}"
    only_quarter = re.search(r"第?([一二三四1-4])季度", text)
    if only_quarter and year:
        return f"{year.group(1)}Q{_QUARTERS[only_quarter.group(1)]}"
    return f"{year.group(1)}" if year else None


def _segment(title: str, full: str, metric: str) -> str:
    if re.search(r"\bAI\s*PC\b|GenAI\s*PC|AI电脑", title, re.IGNORECASE):
        return "ai_pc"
    if re.search(r"二手|翻新|refurbished", title, re.IGNORECASE):
        return "refurbished"
    if metric == "price" and re.search(r"DRAM|NAND|内存|存储|memory", title, re.IGNORECASE):
        return "memory"
    if metric == "price" and re.search(r"DRAM|NAND|内存|存储|memory", full, re.IGNORECASE):
        return "memory"
    return "pc"


def _metric(text: str) -> str | None:
    if re.search(r"出货|shipment", text, re.IGNORECASE):
        return "shipments"
    if re.search(r"渗透率|penetration", text, re.IGNORECASE):
        return "penetration"
    if re.search(r"市场规模|market size|market is projected", text, re.IGNORECASE):
        return "market_size"
    if re.search(r"价格|price|涨价|均价|\bASP\b", text, re.IGNORECASE):
        return "price"
    if re.search(r"收入|revenue", text, re.IGNORECASE):
        return "revenue"
    if re.search(r"份额|share", text, re.IGNORECASE):
        return "share"
    return None


def metric_key(source: dict) -> str | None:
    title = str(source.get("title") or "")
    full = f"{title} {source.get('excerpt', '')}"
    region = "china" if re.search(r"中国|China", full, re.IGNORECASE) else (
        "global" if re.search(r"全球|worldwide|global", full, re.IGNORECASE) else None
    )
    period = _period(title) or _period(full)
    metric = _metric(title) or _metric(full)
    if not (region and period and metric):
        return None
    return f"{region}:{period}:{_segment(title, full, metric)}:{metric}"


def _dedupe(sources: list[dict]) -> tuple[list[dict], list[dict]]:
    kept, duplicates, seen = [], [], {}
    for source in sources:
        if source.get("kind") == "document" and source.get("document_id") and source.get("chunk_id"):
            key = ("document", source.get("document_id"), source.get("chunk_id"))
        else:
            url = (source.get("url") or "").split("#", 1)[0]
            key = ("url", url) if url else ("title", re.sub(r"\s+", "", str(source.get("title") or "")))
        if key in seen:
            duplicates.append({"source_id": source.get("source_id"), "reason": "duplicate"})
            continue
        seen[key] = source
        kept.append(source)
    return kept, duplicates


def evidence_item(source: dict, meta: dict, excerpt_limit: int = 600) -> dict:
    item = {key: source.get(key) for key in (
        "source_id", "kind", "title", "url", "document_id", "chunk_id",
        "page", "score", "source_deleted", "excerpt", "retrieved_at",
    )}
    item["title"] = str(item.get("title") or "")[:120]
    from services.evidence_excerpt import select_excerpt
    item["excerpt"] = select_excerpt(item.get("excerpt"), excerpt_limit)
    item["published_at"] = meta.get("published_at")
    item["role"] = meta.get("role")
    return item


def _item_size(source: dict, meta: dict, excerpt_limit: int = 600) -> int:
    return len(json.dumps(evidence_item(source, meta, excerpt_limit), ensure_ascii=False))


def _novelty(source: dict, selected: list[dict], meta: dict) -> float:
    if not selected:
        return 1.0
    numbers = {value for value, _, _ in parse_number_values(source.get("excerpt", ""))}
    title_grams = _text_grams(str(source.get("title") or ""))
    best = 0.0
    for other in selected:
        other_numbers = {value for value, _, _ in parse_number_values(other.get("excerpt", ""))}
        best = max(best, _jaccard(numbers, other_numbers))
        best = max(best, _jaccard(title_grams, _text_grams(str(other.get("title") or ""))))
    return 1.0 - best


def _score(source: dict, item: dict, selected: list[dict]) -> float:
    return (
        0.35 * item["relevance"]
        + 0.25 * item["freshness"]
        + 0.15 * item["authority"]
        + 0.15 * min(1.0, item["density"])
        + 0.10 * _novelty(source, selected, item)
    )


def _priority(source: dict, meta: dict) -> tuple:
    item = meta[source["source_id"]]
    return (
        not item.get("required", False),
        item["role"] != "actual",
        not item.get("focus", False),
        _date_order(item["published_at"]),
        -item["score"],
    )


def _date_order(value: str | None) -> int:
    """有日期且越新越靠前；无日期排在有日期之后。"""
    if not value:
        return 0
    try:
        return -date.fromisoformat(value).toordinal()
    except ValueError:
        return 0


def rank_sources(query: str, sources: list[dict], budget: int = 12000, *, today: date | None = None,
                 required_ids=None) -> dict:
    """返回 selected / dropped / meta；排序确定，不调用模型。"""
    today = today or date.today()
    required_set = {str(source_id) for source_id in (required_ids or [])}
    valid = [
        source for source in sources
        if source.get("source_id") and not source.get("source_deleted") and str(source.get("excerpt") or "").strip()
    ]
    deduped, duplicates = _dedupe(valid)
    relevance = _relevance_scores(query, deduped)
    meta = {}
    for source in deduped:
        sid = source["source_id"]
        published = parse_published_at(source, today.year)
        role = classify_role(source)
        meta[sid] = {
            "published_at": published.isoformat() if published else None,
            "role": role,
            "authority": authority_score(source),
            "density": density_score(source, role),
            "has_numbers": bool(_substantive_tokens(source)),
            "relevance": relevance.get(sid, 0.5),
            "freshness": freshness_score(published, today),
            "metric_key": metric_key(source),
        }
    for source in deduped:
        meta[source["source_id"]]["score"] = _score(source, meta[source["source_id"]], [])

    low_value = [
        source for source in deduped
        if source.get("kind") != "document" and not meta[source["source_id"]]["has_numbers"]
    ]
    low_value_ids = {source["source_id"] for source in low_value}
    selectable = [source for source in deduped if source["source_id"] not in low_value_ids]
    if not selectable and low_value:
        # 只有低信息量来源时仍要给出可用上下文，不能静默变成空。
        selectable, low_value = low_value, []

    groups: dict[str, list[dict]] = defaultdict(list)
    for source in selectable:
        key = meta[source["source_id"]]["metric_key"]
        if key:
            groups[key].append(source)
    must: list[dict] = []
    for group in groups.values():
        actuals = [source for source in group if meta[source["source_id"]]["role"] == "actual"]
        if actuals:
            must.append(max(actuals, key=lambda source: (
                meta[source["source_id"]]["published_at"] or "",
                meta[source["source_id"]]["score"],
            )))
        must.extend(sorted(group, key=lambda source: meta[source["source_id"]]["score"], reverse=True)[:2])
    if selectable:
        must.append(max(selectable, key=lambda source: meta[source["source_id"]]["score"]))
    must.extend(sorted(
        selectable,
        key=lambda source: meta[source["source_id"]]["published_at"] or "",
        reverse=True,
    )[:2])
    must.extend(_focus_sources(query, selectable, meta))
    required_sources = [
        source for source in deduped
        if source["source_id"] in required_set
    ]
    for source in required_sources:
        meta[source["source_id"]]["required"] = True
    # 必保来源不参与 16 条候选上限，并在预算分配时最先挑选；
    # 否则返工稿会因截断丢失审核要求保留的引用，下一轮变成无法收敛的“丢失原引用”。
    must = [source for source in must if source["source_id"] not in required_set]
    must = list({source["source_id"]: source for source in must}.values())
    must = (sorted(required_sources, key=lambda source: _priority(source, meta))
            + sorted(must, key=lambda source: _priority(source, meta))[:max(0, 16 - len(required_sources))])

    selected: list[dict] = []
    dropped: list[dict] = list(duplicates)
    remaining = max(0, budget - 512)
    for source in must:
        for limit in (400, 300, 200):
            size = _item_size(source, meta[source["source_id"]], limit)
            if size <= remaining:
                selected.append(source)
                meta[source["source_id"]]["excerpt_limit"] = limit
                remaining -= size
                break
        else:
            dropped.append({"source_id": source["source_id"], "reason": "budget"})

    pool = [source for source in selectable if source not in selected]
    while pool:
        candidates = [
            source for source in pool
            if _item_size(source, meta[source["source_id"]], 200) <= remaining
        ]
        if not candidates:
            break
        best = max(candidates, key=lambda source: _score(source, meta[source["source_id"]], selected))
        best_score = _score(best, meta[best["source_id"]], selected)
        if best_score < 0.20 and selected:
            break
        for limit in (400, 300, 200):
            size = _item_size(best, meta[best["source_id"]], limit)
            if size <= remaining:
                selected.append(best)
                meta[best["source_id"]]["excerpt_limit"] = limit
                remaining -= size
                break
        pool.remove(best)

    if not selected and selectable:
        fallback = max(selectable, key=lambda source: meta[source["source_id"]]["score"])
        if _item_size(fallback, meta[fallback["source_id"]]) <= remaining:
            selected.append(fallback)

    selected_ids = {source["source_id"] for source in selected}
    for source in deduped:
        sid = source["source_id"]
        if sid in selected_ids:
            continue
        if any(item["source_id"] == sid for item in dropped):
            continue
        if sid in low_value_ids:
            reason = "low_value"
        else:
            reason = "low_value" if meta[sid]["score"] < 0.20 else "low_relevance"
        dropped.append({"source_id": sid, "reason": reason})

    selected.sort(key=lambda source: _priority(source, meta))
    return {"selected": selected, "dropped": dropped, "meta": meta}


def required_evidence_context(query, sources, required_ids, budget=12000, ceiling=64000):
    """必需来源保留完整快照；可扩展到硬上限，超限显式报错，不静默漏传。"""
    required = set(required_ids or [])
    known = {s['source_id']: s for s in sources if s.get('source_id') and not s.get('source_deleted')}
    selected = [dict(known[sid]) for sid in sorted(required) if sid in known]
    context = {'untrusted': True, 'sources': selected, 'selection': {
        'selected': [s['source_id'] for s in selected], 'dropped': [],
        'missing_required': sorted(required - known.keys()),
    }}
    if len(json.dumps(context, ensure_ascii=False)) > ceiling:
        raise ValueError('EVIDENCE_CONTEXT_OVERFLOW：完整必需证据超过上下文硬上限，未调用模型，不能据此判定来源不支持')
    ranked = rank_sources(query, sources, budget=budget)
    for source in ranked['selected']:
        if source['source_id'] in required:
            continue
        meta = ranked['meta'][source['source_id']]
        item = evidence_item(source, meta, meta.get('excerpt_limit', 600))
        candidate = dict(context, sources=selected + [item])
        if len(json.dumps(candidate, ensure_ascii=False)) <= min(budget, ceiling):
            selected.append(item)
    selected_ids = {s['source_id'] for s in selected}
    context['selection']['selected'] = sorted(selected_ids)
    # 非必需来源只是补充，不能挤占已引用来源；不附加无界的排除列表。
    context['selection']['dropped'] = [
        {'source_id': s['source_id'], 'reason': 'optional_not_selected'}
        for s in sources if s.get('source_id') not in selected_ids][:32]
    if len(json.dumps(context, ensure_ascii=False)) > ceiling:
        context['selection']['dropped'] = []
    return context


def review_evidence_block(query: str, sources: list[dict], budget: int = 12000, selection: dict | None = None,
                          required_ids=None) -> str:
    """审核员看到的证据摘要：优先保证草稿实际引用的来源，再按相关性补足。"""
    if not sources:
        return "可用证据：无。"
    # 草稿引用的来源必须进审核上下文，否则审核员会把“自己没看到”误判成“资料不支持”。
    if required_ids:
        context = required_evidence_context(query, sources, required_ids, budget)
        lines = ['可用证据（已引用来源提供完整快照；其余为补充资料）：']
        for source in context['sources']:
            lines.append(f"[{source['source_id']}] {source.get('title', '')} | 完整性={source.get('excerpt_status', 'unverified')} | {source.get('excerpt', '')}")
        if context['selection']['missing_required']:
            lines.append('来源不存在或已删除：' + '、'.join(context['selection']['missing_required']))
        return '\n'.join(lines)
    ranked = rank_sources(query, sources, budget=budget, required_ids=required_ids)
    lines = ["可用证据（按与草稿的相关性排序，摘录可能截断）："]
    for source in ranked["selected"]:
        meta = ranked["meta"][source["source_id"]]
        lines.append(
            f"[{source['source_id']}] {meta.get('published_at') or '日期未知'} "
            f"{meta.get('role')} | {source.get('title', '')} | {evidence_item(source, meta, 500)['excerpt']}"
        )
    if selection:
        selected = selection.get("selected") or []
        dropped = selection.get("dropped") or []
        if selected:
            lines.append(f"writer 实际使用：{', '.join(selected)}")
        if dropped:
            preview = "；".join(
                f"{item.get('source_id')}（{item.get('reason')}）" for item in dropped[:8]
            )
            lines.append(f"writer 未使用：{preview}")
    return "\n".join(lines)
