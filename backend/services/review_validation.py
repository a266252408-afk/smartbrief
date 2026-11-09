"""审核响应原文/出处/坐标验证，不能代替语义判断。"""

import hashlib
import re
from typing import Literal
from pydantic import Field
from services.review_contracts import Contract
from services.rewrite_patches import paragraph_table
from services.citations import split_sentences


class ReviewValidationIssue(Contract):
    code: str
    path: str
    message: str


class ReviewResponseAttempt(Contract):
    stage: Literal["response_validation"]
    response_id: str | None
    model: str | None
    finish_reason: str | None
    raw_response: str
    raw_length: int = Field(ge=0)
    raw_truncated: bool
    raw_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    decision: Literal["accepted", "rejected"] | None = None
    code: str | None = None
    message: str | None = None


def finding_errors(review, body, evidence, *, task_id=None):
    paragraphs = paragraph_table(body)
    sources = {
        s["source_id"]: s
        for s in evidence
        if not s.get("source_deleted")
        and (task_id is None or s.get("task_id") == task_id)
    }
    errors = []
    for index, finding in enumerate(review.get("findings", [])):
        base = f"/findings/{index}"

        def error(code, path, message):
            errors.append(
                ReviewValidationIssue(
                    code=code, path=base + path, message=message
                ).model_dump()
            )

        position = finding.get("location") or {}
        n, sentence = position.get("paragraph"), position.get("sentence")
        if type(n) is not int or not 1 <= n <= len(paragraphs):
            error("REPORT_LOCATION_INVALID", "/location/paragraph", "报告段落不存在")
            continue
        sentences = split_sentences(paragraphs[n - 1]["text"])
        if type(sentence) is not int or not 1 <= sentence <= len(sentences):
            error("REPORT_LOCATION_INVALID", "/location/sentence", "报告句子不存在")
            continue
        source = finding.get("evidence") or {}
        quote = source.get("quote")
        if source.get("kind", "source") == "report":
            text = paragraphs[n - 1]["text"]
            offset = 0
            for part in sentences[:sentence]:
                start = text.find(part, offset)
                offset = start + len(part)
            spans = (
                list(re.finditer(re.escape(quote), text))
                if isinstance(quote, str) and quote
                else []
            )
            overlap = any(max(start, m.start()) < min(offset, m.end()) for m in spans)
            if not spans or (finding.get("scope") != "full" and not overlap):
                error(
                    "REPORT_QUOTE_INVALID", "/evidence/quote", "报告原句不属于指定位置"
                )
        else:
            actual = sources.get(source.get("source_id"))
            if actual is None:
                error(
                    "SOURCE_UNAVAILABLE",
                    "/evidence/source_id",
                    "来源不存在、已删除或跨任务",
                )
            elif (
                not isinstance(quote, str)
                or not quote
                or quote not in actual.get("excerpt", "")
            ):
                error(
                    "SOURCE_QUOTE_INVALID", "/evidence/quote", "证据原句不在来源正文中"
                )
    return errors


def repair_report_locations(review, body):
    """报告finding的quote若唯一存在，确定性修正段/句坐标；不改quote或判定。"""
    paragraphs = paragraph_table(body)
    corrections = []
    for finding in review.get("findings", []):
        source = finding.get("evidence") or {}
        if source.get("kind", "source") != "report":
            continue
        quote = source.get("quote")
        located = [
            (row["paragraph"], row["text"])
            for row in paragraphs
            if isinstance(quote, str) and quote and quote in row["text"]
        ]
        if len(located) != 1:
            continue
        paragraph, text = located[0]
        offset = text.index(quote)
        sentence = 1
        cursor = 0
        for index, part in enumerate(split_sentences(text), 1):
            start = text.find(part, cursor)
            if start < 0:
                break
            cursor = start + len(part)
            sentence = index
            if start <= offset < cursor:
                break
        current = finding.get("location") or {}
        if (current.get("paragraph"), current.get("sentence")) != (paragraph, sentence):
            finding["location"] = dict(paragraph=paragraph, sentence=sentence)
            corrections.append(
                dict(
                    finding_id=finding.get("finding_id"),
                    before=current,
                    after=finding["location"],
                )
            )
    return corrections


def response_audit(response):
    choices = getattr(response, "choices", None) or []
    choice = choices[0] if choices else None
    raw = getattr(getattr(choice, "message", None), "content", None) or ""
    if not isinstance(raw, str):
        raw = repr(raw)
    return ReviewResponseAttempt(
        stage="response_validation",
        response_id=getattr(response, "id", None),
        model=getattr(response, "model", None),
        finish_reason=getattr(choice, "finish_reason", None),
        raw_response=raw[:32000],
        raw_length=len(raw),
        raw_truncated=len(raw) > 32000,
        raw_sha256=hashlib.sha256(raw.encode()).hexdigest(),
    ).model_dump(exclude_unset=True)
