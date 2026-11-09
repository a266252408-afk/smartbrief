"""v3最小报告文档：输入仅内容，身份/定位/引用由程序维护。"""

from typing import Literal, Annotated
from pydantic import Field, StringConstraints, AfterValidator
from services.review_contracts import Contract, Text
import hashlib
import json
import re
from uuid import uuid4
from services.fact_binding import build_registry, render_bound_text, _embedded_fact
from services.citations import quantitative_tokens, split_sentences


def _nonblank_content(value):
    if not value.strip():
        raise ValueError("内容不能只包含空白")
    return value


RawText = Annotated[
    str, StringConstraints(strict=True, min_length=1), AfterValidator(_nonblank_content)
]


class ModelSegment(Contract):
    text: RawText
    facts: list[Text] = Field(default_factory=list)
    premises: list[Text] = Field(default_factory=list)


class ModelBlock(Contract):
    kind: Literal["transition", "fact", "analysis", "recommendation"]
    segments: list[ModelSegment] = Field(min_length=1)


class ModelSection(Contract):
    role: Literal["summary", "body", "conclusion"]
    title: Text
    blocks: list[ModelBlock] = Field(min_length=1)


class ModelDocument(Contract):
    title: Text
    sections: list[ModelSection] = Field(min_length=1)


class DocumentClaim(Contract):
    claim_id: Text
    quote: RawText
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    fact_ids: list[Text]
    premise_ids: list[Text]
    source_ids: list[Text]


class DocumentBlock(Contract):
    block_id: Text
    kind: Literal["transition", "fact", "analysis", "recommendation"]
    text: RawText
    claims: list[DocumentClaim]


class DocumentSection(Contract):
    section_id: Text
    role: Literal["summary", "body", "conclusion"]
    title: Text
    blocks: list[DocumentBlock] = Field(min_length=1)


class ReportDocument(Contract):
    version: Literal[3] = 3
    task_id: Text
    revision: int = Field(ge=1)
    registry_revision: Text
    title: Text
    sections: list[DocumentSection] = Field(min_length=1)
    sources: dict[str, dict]


class DocumentError(ValueError):
    code = "DOCUMENT_INVALID"

    def __init__(self, message, path="/"):
        self.path = path
        super().__init__(message)


def fact_context(task_id, evidence):
    registry = build_registry(task_id, evidence)
    digest = hashlib.sha256(
        json.dumps(registry, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return dict(
        task_id=task_id,
        registry_revision=digest,
        registry=registry,
        aliases={f"F{n}": key for n, key in enumerate(sorted(registry), 1)},
    )


def _prose(text):
    return re.sub(
        r"(?ms)^```[^\n]*\n.*?^```[^\n]*$|^~~~[^\n]*\n.*?^~~~[^\n]*$", "", text
    )


def _claim_check(quote, ids, registry, task_id):
    if ids:
        if any(key not in registry for key in ids):
            raise DocumentError("声明事实不存在或跨任务")
        for sentence in split_sentences(quote):
            _literal_claim_scope(sentence, [registry[key]['quote'] for key in ids])
            render_bound_text(
                sentence + "".join(f"[{key}]" for key in ids), task_id, registry
            )
    elif quantitative_tokens(_prose(quote)):
        raise DocumentError("定量声明缺少事实绑定")


def _literal_claim_scope(sentence, facts):
    """只检查可对齐的单句：极性翻转及简单指标主体替换，不猜一般语义。"""
    def compact(text):
        return re.sub(r'\s|[。.!！]', '', text)
    value = compact(sentence)
    originals = [compact(fact) for fact in facts]
    if value in originals:
        return
    # 去掉否定词后逐字相同才能证明翻转；不能把“不低于”当作普通否定。
    polarity = r'并非|并不|不是|没有'
    neutral = re.sub(polarity, '', value)
    if any(neutral == re.sub(polarity, '', original)
           and re.findall(polarity, value) != re.findall(polarity, original)
           for original in originals):
        raise DocumentError("声明与绑定事实的肯否极性不一致")
    # 限定为完整单指标句。一般改写/多事实论证仍交既有语义审核。
    pattern = r'((?:19|20)\d{2}年)(中国|全球|欧洲|北美)([\u4e00-\u9fff]{2,24}?)(销售额|出口量|出货量|销量)(\d+(?:\.\d+)?(?:万|亿)?(?:元|台|册))'
    claim = re.fullmatch(pattern, value)
    if claim:
        aligned = [re.fullmatch(pattern, original) for original in originals]
        aligned = [match for match in aligned if match and
                   (match[1], match[2], match[4], match[5]) == (claim[1], claim[2], claim[4], claim[5])]
        if aligned and all(match[3] != claim[3] for match in aligned):
            raise DocumentError("声明的统计对象无法由绑定事实核验")


def build_document(payload, task_id, evidence, context=None, *, revision=1):
    source = ModelDocument.model_validate(payload)
    from services.evidence import validate_sources

    sources = validate_sources(task_id, evidence)
    expected = fact_context(task_id, evidence)
    ctx = expected if context is None else context
    if ctx != expected:
        raise DocumentError("事实上下文跨任务、过期或被修改")
    sections = []
    snapshots = {}
    for si, section in enumerate(source.sections):
        blocks = []
        for bi, block in enumerate(section.blocks):
            text = "".join(segment.text for segment in block.segments)
            claims = []
            offset = 0
            for ci, segment in enumerate(block.segments):
                quote = segment.text
                start = offset
                offset += len(quote)

                def resolve(alias):
                    if alias not in ctx["aliases"]:
                        raise DocumentError("事实短别名无效")
                    return ctx["aliases"][alias]

                facts = [resolve(a) for a in segment.facts]
                premises = [resolve(a) for a in segment.premises]
                ids = list(dict.fromkeys(facts + premises))
                if (
                    not ids
                    and not quantitative_tokens(_prose(quote))
                    and not _embedded_fact(_prose(quote))
                ):
                    continue
                _claim_check(quote, ids, ctx["registry"], task_id)
                sids = sorted({ctx["registry"][key]["source_id"] for key in ids})
                for key in ids:
                    fact = ctx["registry"][key]
                    snapshots[fact["source_id"]] = next(
                        s for s in sources if s["source_id"] == fact["source_id"]
                    )
                claims.append(
                    DocumentClaim(
                        claim_id="C" + uuid4().hex,
                        quote=quote,
                        start=start,
                        end=offset,
                        fact_ids=facts,
                        premise_ids=premises,
                        source_ids=sids,
                    )
                )
            blocks.append(
                DocumentBlock(
                    block_id="B" + uuid4().hex,
                    kind=block.kind,
                    text=text,
                    claims=claims,
                )
            )
        sections.append(
            DocumentSection(
                section_id="S" + uuid4().hex,
                role=section.role,
                title=section.title,
                blocks=blocks,
            )
        )
    document = ReportDocument(
        task_id=task_id,
        revision=revision,
        registry_revision=ctx["registry_revision"],
        title=source.title,
        sections=sections,
        sources=snapshots,
    ).model_dump()
    validate_document(document, task_id)
    return document


def validate_document(document, task_id=None):
    doc = ReportDocument.model_validate(document)
    if task_id is not None and doc.task_id != task_id:
        raise DocumentError("文档不属于当前任务")
    if "\n" in doc.title or doc.title.startswith("#"):
        raise DocumentError("报告标题必须为单行纯标题")
    roles = [s.role for s in doc.sections]
    if (
        roles.count("summary") != 1
        or roles.count("conclusion") != 1
        or "body" not in roles
    ):
        raise DocumentError("文档必须包含唯一摘要、正文及唯一结论")
    registry = build_registry(doc.task_id, list(doc.sources.values()))
    from services.evidence import validate_sources

    validate_sources(doc.task_id, list(doc.sources.values()))
    if any(sid != s.get("source_id") for sid, s in doc.sources.items()):
        raise DocumentError("来源字典身份不一致")
    seen = set()
    for section in doc.sections:
        if "\n" in section.title or section.title.startswith("#"):
            raise DocumentError("章节标题必须是单行")
        for identifier in [section.section_id] + [b.block_id for b in section.blocks]:
            if identifier in seen:
                raise DocumentError("内部身份重复")
            seen.add(identifier)
        for block in section.blocks:
            if re.search(r"(?m)^#{1,6}\s+", _prose(block.text)):
                raise DocumentError("块正文不能夹带章节标题")
            end = 0
            uncovered = block.text
            for claim in sorted(block.claims, key=lambda c: c.start):
                if claim.claim_id in seen:
                    raise DocumentError("声明身份重复")
                seen.add(claim.claim_id)
                if (
                    claim.start < end
                    or claim.end > len(block.text)
                    or block.text[claim.start : claim.end] != claim.quote
                ):
                    raise DocumentError("声明范围不一致或重叠")
                end = claim.end
                ids = list(dict.fromkeys(claim.fact_ids + claim.premise_ids))
                _claim_check(claim.quote, ids, registry, doc.task_id)
                if claim.source_ids != sorted(
                    {registry[key]["source_id"] for key in ids}
                ):
                    raise DocumentError("声明来源映射不一致")
                uncovered = uncovered.replace(claim.quote, "", 1)
            if quantitative_tokens(_prose(uncovered)) or _embedded_fact(
                _prose(uncovered)
            ):
                raise DocumentError("正文存在未绑定的定量声明")
    return doc.model_dump()


def render_parts(document):
    doc = ReportDocument.model_validate(validate_document(document))
    parts = [(None, "# " + doc.title)]
    ordered = sorted(
        enumerate(doc.sections),
        key=lambda pair: (
            {"summary": 0, "body": 1, "conclusion": 2}[pair[1].role],
            pair[0],
        ),
    )
    for _, section in ordered:
        title = {"summary": "执行摘要", "conclusion": "结论与建议"}.get(
            section.role, section.title
        )
        parts.append((None, "## " + title))
        for block in section.blocks:
            text = block.text
            for claim in sorted(block.claims, key=lambda c: c.start, reverse=True):
                text = (
                    text[: claim.end]
                    + "".join("[" + sid + "]" for sid in claim.source_ids)
                    + text[claim.end :]
                )
            parts.append((block.block_id, text))
    if doc.sources:
        parts.append((None, "## 来源附录"))
        for sid, s in sorted(doc.sources.items()):
            locator = s.get("url") or f"文档 {s['document_id']}" + (
                f"，第{s['page']}页" if s.get("page") else ""
            )
            parts.append((None, f"- [{sid}] {s['title']} — {locator}"))
    return parts


def render_document(document):
    return "\n\n".join(text for _, text in render_parts(document))


def narrative_view(document):
    """保留段落分隔，代码与来源附录不参与正文事实判定。"""
    body = render_document(document)

    def mask(match):
        return re.sub(r"[^\s]", "·", match.group(0))

    body = re.sub(
        r"(?ms)^```[^\n]*\n.*?^```[ \t]*$|^~~~[^\n]*\n.*?^~~~[ \t]*$", mask, body
    )
    body = re.sub(r"(`+)[^\n]*?\1", mask, body)
    marker = body.find("\n\n## 来源附录\n\n")
    if marker >= 0:
        body = body[:marker] + re.sub(r"[^\s]", "·", body[marker:])
    return body
