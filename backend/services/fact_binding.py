"""任务内事实原句与引用映射；模型使用事实编号，程序渲染来源。"""
import hashlib
import re
from pydantic import BaseModel, ConfigDict, Field
from services.evidence import Evidence

from services.citations import split_sentences, check_citations, quantitative_tokens
from services.claim_semantics import object_context, period_context, geography_context
from services.facts import fact_table, FORECAST
from services.numbers import token_matches_text

FACT_REF = re.compile(r"\[(F[A-Za-z0-9]+)\]")


def _normalize_fact_sentence(sentence: str) -> str:
    """来源摘要可能带行首 Markdown 标题标记；事实原句必须与正文使用同一文本口径。"""
    return re.sub(r"^\s*#{1,6}\s*", "", sentence or "").strip()


def _edit_distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 1:
        return 2
    if len(a) > len(b):
        a, b = b, a
    previous = list(range(len(a) + 1))
    for row, char in enumerate(b, 1):
        current = [row]
        for col, other in enumerate(a, 1):
            current.append(min(previous[col] + 1, current[col - 1] + 1,
                               previous[col - 1] + (char != other)))
        previous = current
    return previous[-1]


def _resolve_fact_key(key: str, registry: dict) -> str:
    """编号抄错一位时，若只有一个编辑距离 1 的候选才纠正；否则交给调用方报错。"""
    if key in registry:
        return key
    near = [candidate for candidate in registry if _edit_distance(key, candidate) == 1]
    return near[0] if len(near) == 1 else key


class BindingError(ValueError):
    """事实绑定校验失败；记录违规句子，供写手定向修复，而不是丢弃整稿。"""

    def __init__(self, reason: str, sentence: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.sentence = sentence


class BoundFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fact_id: str = Field(pattern=r"^F[a-f0-9]{24}$")
    task_id: str
    source_id: str
    quote: str
    source: Evidence
    measurements: list[dict] = Field(default_factory=list)


class ClaimBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    paragraph: int = Field(ge=1)
    text: str
    fact_ids: list[str]
    source_ids: list[str]


def build_registry(task_id, sources):
    registry = {}
    for source in sources:
        if source.get("task_id") != task_id or source.get("source_deleted"):
            continue
        for sentence in split_sentences(source.get("excerpt", "")):
            sentence = _normalize_fact_sentence(sentence)
            if not sentence or re.search(r"\d[\d.,]*$", sentence):
                continue
            key = "F" + hashlib.sha256(
                (task_id + "\0" + source["source_id"] + "\0" + sentence).encode()).hexdigest()[:24]
            registry[key] = BoundFact(fact_id=key, task_id=task_id, source_id=source["source_id"],
                                 quote=sentence, source=dict(source, excerpt=sentence),
                                 measurements=fact_table([dict(source, excerpt=sentence)])["facts"]).model_dump()
    return registry


def binding_prompt(registry):
    return ("引用协议：按顺序输出Markdown段落，标题行无需编号。正文每一句都必须带下列事实编号[F…]；"
            "不带编号的句子只能以下列词开头：建议、推断、假设、资料说明、资料用于、数据待补充、以下为、"
            "本报告仅、未取得，否则视为无支持声明。禁止自行填写[S编号]，也不得编造未列出的编号。"
            "数字、单位、期间、地区、统计对象与原句保持一致；"
            "推断须明确写为推断，不得将相关性改为因果。没有对应事实时省略断言。\n"
            + "\n".join(f"[{key}] {value['quote']}" for key, value in registry.items()))


def _embedded_fact(text):
    return bool(re.search(r'排名第[一二三四五六七八九十0-9]+|位居第[一二三四五六七八九十0-9]+|仅次于|(?:份额|规模|销量|收入).{0,8}(?:最高|最大|第一)', text))


def _organizational_transition(text):
    """匹配完整组织句及其分析维度，不按句首前缀放行外部事实。"""
    value = (text or '').strip()
    if _embedded_fact(value) or re.search(r'已经|已达|达到|突破|(?:持续|显著|快速)增长|同比|环比', value):
        return False
    without_count = re.sub(r'[一二三四五六七八九十0-9]+个方面', '方面', value)
    if quantitative_tokens(without_count):
        return False
    return bool(re.fullmatch(
        r'(?:下文|以下|本文|本报告|接下来)(?:将|会)?(?:从[\u4e00-\u9fff、，,与和及\s]{2,60}方面(?:展开|进行)分析|依次讨论[\u4e00-\u9fff、，,与和及\s]{2,60})[。.!！]?', without_count))


def render_bound_text(body, task_id, registry, *, previous=""):
    fenced = re.split(r"(?ms)(^```[^\n]*\n.*?^```[ \t]*$|^~~~[^\n]*\n.*?^~~~[ \t]*$)", body)
    if len(fenced) > 1:
        chunks, all_claims = [], []
        for index, chunk in enumerate(fenced):
            if index % 2:
                chunks.append(chunk)
            else:
                text, rows = render_bound_text(chunk, task_id, registry, previous=previous)
                chunks.append(text)
                all_claims.extend(rows)
        rendered = "".join(chunks)
        return rendered, locate_bindings(rendered, all_claims)
    claims = []
    def prose(text):
        return re.sub(r"(?m)^#{1,6}[^\n]*(?:\n|$)", "", text).strip()
    previous_claims = {prose(s) for p in re.split(r"\n\s*\n", previous) for s in split_sentences(p)}
    parts = re.split(r"(\n\s*\n)", body)
    for index in range(0, len(parts), 2):
        paragraph = parts[index]
        rendered = []
        for sentence in split_sentences(paragraph):
            ids = FACT_REF.findall(sentence)
            if not ids:
                statement = prose(sentence)
                preserved = bool(statement and statement in previous_claims)
                if re.search(r"\[S\d+\]", sentence) and not preserved:
                    raise BindingError("新声明必须提供事实编号", sentence)
                if _organizational_transition(statement):
                    rendered.append(sentence)
                    continue
                if _embedded_fact(statement) and not preserved:
                    raise BindingError("排序事实声明缺少事实编号", sentence)
                if quantitative_tokens(statement) and not preserved:
                    raise BindingError("定量声明缺少事实编号", sentence)
                if statement and not preserved and not re.match(
                        r"^(?:建议|推断|假设|资料说明|资料用于|数据待补充|以下为|本报告仅|未取得)", statement):
                    raise BindingError("定性声明缺少事实编号或明确的建议/推断标识", sentence)
                rendered.append(sentence)
                continue
            facts = []
            resolved = {}
            corrections = {}
            for key in ids:
                matched = _resolve_fact_key(key, registry)
                if matched != key:
                    corrections[key] = matched
                resolved[key] = matched
                fact = registry.get(matched)
                if fact is None or fact["task_id"] != task_id:
                    raise BindingError("事实编号不存在或归属错误", sentence)
                facts.append(fact)
            sources = {}
            for fact in facts:
                sid = fact["source_id"]
                if sid not in sources:
                    sources[sid] = dict(fact["source"])
                elif fact["quote"] not in sources[sid]["excerpt"]:
                    sources[sid]["excerpt"] += "\n" + fact["quote"]
            clean = FACT_REF.sub(
                lambda m: "[" + registry[resolved.get(m[1], m[1])]["source_id"] + "]", sentence)
            claim_text = FACT_REF.sub("", prose(sentence))
            context = "\n".join(f["quote"] + "\n" + f["source"].get("title", "") for f in facts)
            claim_period, known_period = period_context(claim_text), period_context(context)
            if any(set(claim_period[field]) - set(known_period[field]) for field in ("years", "periods")):
                raise BindingError("声明与绑定事实期间不一致或补造未知期间", sentence)
            if set(geography_context(claim_text)) - set(geography_context(context)):
                raise BindingError("声明与绑定事实地区不一致或补造未知地区", sentence)
            for token in quantitative_tokens(claim_text):
                records = [m for fact in facts for m in fact["measurements"]
                           if token_matches_text(token, str(m["value"]) + m["unit"])]
                if records and claim_period["years"] and all(
                        m["year"] is not None and m["year"] not in claim_period["years"] for m in records):
                    raise BindingError("该数值对应的期间与声明不一致", sentence)
                if records and all(m["role"] == "forecast" for m in records) and not (
                        FORECAST.search(claim_text) or "有望" in claim_text):
                    raise BindingError("预测事实必须保留预测或预计标签", sentence)
            measurements = [m for fact in facts for m in fact["measurements"]
                            if token_matches_text(str(m["value"]) + m["unit"], claim_text)
                            and (not claim_period["years"] or m["year"] in claim_period["years"])]
            roles = {m["role"] for m in measurements}
            if roles == {"forecast"} and not (FORECAST.search(claim_text) or "有望" in claim_text):
                raise BindingError("预测事实必须保留预测或预计标签", sentence)
            check = check_citations(task_id, clean, list(sources.values()))
            from services.review_rules import is_confirmed_blocker
            if check["errors"] or any(is_confirmed_blocker(i) or (i.get("kind") in {"unbound_number", "merged_fact_range"} and i.get("severity") == "hard") for i in check["binding_issues"]):
                raise BindingError("声明与绑定事实不一致", sentence)
            objects = set().union(*(set(object_context(f["quote"]) or object_context(f["source"].get("title", "")))
                                    for f in facts))
            if set(object_context(claim_text)) - objects:
                raise BindingError("声明改变或补造了绑定事实的统计对象", sentence)
            claim = dict(paragraph=index // 2 + 1, text=prose(clean),
                         fact_ids=[resolved.get(key, key) for key in ids], source_ids=sorted(sources))
            if corrections:
                claim["fact_id_corrections"] = corrections
            claims.append(claim)
            rendered.append(clean)
        parts[index] = "".join(rendered)
    return "".join(parts), claims


def salvage_bound_text(body, task_id, registry, *, previous="", max_drops=12):
    """逐句删除无法绑定的事实性声明后重新渲染，返回 (正文, claims, 已删除原句)。

    只在模型定向修复仍不通过时使用：协议要求“没有对应事实时省略断言”，
    但删除有上限，避免把正文删成空壳后仍当作成功稿。
    """
    dropped = []
    text = body
    while True:
        try:
            rendered, claims = render_bound_text(text, task_id, registry, previous=previous)
            return rendered, claims, dropped
        except BindingError as exc:
            if not exc.sentence or len(dropped) >= max_drops:
                raise
            index = text.find(exc.sentence)
            if index < 0:
                raise
            # 标题行是结构，不是声明；删句时必须保留，避免把整节标题一起删掉。
            headings = [line for line in exc.sentence.splitlines()
                        if re.match(r"^\s*#{1,6}\s+\S", line)]
            text = text[:index] + "\n".join(headings) + text[index + len(exc.sentence):]
            dropped.append(exc.sentence)


def locate_bindings(body, bindings):
    """结构整理后重新确定位置，重复声明逐位置保留。"""
    rows = []
    for n, paragraph in enumerate(re.split(r"\n\s*\n", body), 1):
        seen = set()
        for claim in bindings:
            identity = (claim["text"], tuple(claim["fact_ids"]))
            if claim["text"] in paragraph and identity not in seen:
                rows.append(dict(claim, paragraph=n))
                seen.add(identity)
    return rows
