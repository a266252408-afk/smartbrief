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


def _normalize_quote(text):
    return re.sub(r'\s+', '', text or '')


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
                or _normalize_quote(quote) not in _normalize_quote(actual.get("excerpt", ""))
            ):
                error(
                    "SOURCE_QUOTE_INVALID", "/evidence/quote", "证据原句不在来源正文中"
                )
    return errors


def _ref_free(text):
    return _normalize_quote(re.sub(r'\[S\d+\]', '', text or ''))


def structure_finding_errors(review, body):
    """model 的“必需章节缺失/为空”意见必须与程序结构检查一致。

    标题与正文之间有空行时，审核模型容易只看标题所在段落而误判空章节；
    程序按同级标题分段核对正文，矛盾的意见交回重试，不进入返工。
    """
    from services.report_quality import structure_gaps
    gaps = {gap["kind"] for gap in structure_gaps(body)}
    errors = []
    for index, finding in enumerate(review.get("findings") or []):
        if finding.get("severity") != "hard":
            continue
        problem = str(finding.get("problem") or "")
        for kind, label in (("missing_conclusion", "结论与建议"), ("missing_summary", "执行摘要")):
            if label in problem and re.search(r"缺少|缺失|为空|空章节|仅包含标题|无任何内容", problem):
                blockers = {kind, kind.replace("missing_", "empty_")}
                if not blockers & gaps:
                    errors.append(dict(
                        code="STRUCTURE_FINDING_CONTRADICTED",
                        path=f"/findings/{index}/problem",
                        message=f"程序结构检查显示{label}存在且有正文",
                    ))
    return errors


def repair_source_finding_locations(review, body):
    """来源型 finding 的定位应指向真正引用该来源的报告句子。

    审核给出的位置可能与问题描述的不是同一句（例如问题讲 S4，位置却落在 S2 的句子），
    会让裁决与返工对着错误句子工作。仅当唯一命中时确定性修正。
    """
    paragraphs = paragraph_table(body)
    corrections = []
    for finding in review.get("findings") or []:
        source = finding.get("evidence") or {}
        if source.get("kind", "source") != "source":
            continue
        cited = finding.get("cited") or []
        source_id = source.get("source_id") or (cited[0] if cited else None)
        if source_id is None:
            continue
        ref = f"[{source_id}]"
        location = finding.get("location") or {}
        n, s = location.get("paragraph", 0), location.get("sentence", 1)
        if type(n) is int and 1 <= n <= len(paragraphs):
            pieces = split_sentences(paragraphs[n - 1]["text"])
            if type(s) is int and 1 <= s <= len(pieces) and ref in pieces[s - 1]:
                continue
        matches = [
            (row["paragraph"], index)
            for row in paragraphs
            for index, piece in enumerate(split_sentences(row["text"]), 1)
            if ref in piece
        ]
        if len(matches) != 1:
            continue
        before = dict(location)
        finding["location"] = dict(paragraph=matches[0][0], sentence=matches[0][1])
        corrections.append(
            dict(
                finding_id=finding.get("finding_id"),
                before=before,
                after=finding["location"],
            )
        )
    return corrections


def repair_report_locations(review, body):
    """报告finding的quote若可唯一确定，确定性修正段/句坐标；不改判定。

    quote 与正文只有引用标号/空白差异时，采用正文实际原句，避免审核抄错 [S#]
    让整轮作废；正文用词不同的 quote 仍判无效。
    """
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
        if not located:
            key = _ref_free(quote)
            matches = [
                (row["paragraph"], index, part)
                for row in paragraphs
                for index, part in enumerate(split_sentences(row["text"]), 1)
                if key and _ref_free(part) == key
            ]
            if len(matches) != 1:
                continue
            paragraph, sentence, actual = matches[0]
            before = dict(finding.get("location") or {})
            source["quote"] = actual
            finding["location"] = dict(paragraph=paragraph, sentence=sentence)
            corrections.append(
                dict(
                    finding_id=finding.get("finding_id"),
                    quote_corrected=True,
                    before=before,
                    after=finding["location"],
                )
            )
            continue
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
