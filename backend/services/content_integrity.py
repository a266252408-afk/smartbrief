"""内容覆盖与安全重复装配检查，程序定位不冒充语义判断。"""

import copy
import json
import re
from services.review_contracts import CoreCoverage, SemanticDuplicate


class ContentRejected(ValueError):
    code = "CONTENT_COVERAGE_REJECTED"


def _question_text(value):
    return re.sub(r"\s+", "", value)


def _align_core_rows(rows, questions):
    frozen = [_question_text(question) for question in questions]
    given = [_question_text(row["question"]) for row in rows]
    if len(rows) == len(questions) and set(given) == set(frozen):
        canonical = {_question_text(question): question for question in questions}
        for row in rows:
            row["question"] = canonical[_question_text(row["question"])]
        return rows, True
    # 模型可能把一条冻结问题按完整子句拆成多行；按顺序能逐字拼回原问题才算完整。
    merged = {}
    aligned = []
    index = offset = 0
    complete = True
    for row, text in zip(rows, given):
        if index >= len(frozen) or not text or not frozen[index].startswith(text, offset):
            complete = False
            break
        aligned.append(row)
        question = questions[index]
        offset += len(text)
        if offset == len(frozen[index]):
            index += 1
            offset = 0
        current = merged.get(question)
        if current is None:
            merged[question] = dict(row, question=question)
        else:
            current["covered"] = bool(current["covered"] and row["covered"])
            if row["reason"] not in current["reason"]:
                current["reason"] = current["reason"] + "；" + row["reason"]
    if index != len(frozen) or offset != 0:
        complete = False
    if complete:
        return [merged[question] for question in questions if question in merged], True
    # 不完整覆盖保留已验证的片段供下游按covered判断，整体标记为unknown。
    return aligned, False


def validate_review_content(review, body, questions):
    rows = [
        CoreCoverage.model_validate(r).model_dump()
        for r in review.get("core_coverage", [])
    ]
    duplicates = [
        SemanticDuplicate.model_validate(r).model_dump()
        for r in review.get("semantic_duplicates", [])
    ]
    for row in rows:
        if row["quote"] not in body:
            raise ValueError("核心回答原句不在报告中")
    complete = bool(rows)
    if rows:
        rows, complete = _align_core_rows(rows, questions)
    for row in duplicates:
        if row["first_quote"] not in body or row["second_quote"] not in body:
            raise ValueError("语义重复必须提供报告中的两处原句")
        if (
            row["first_quote"] == row["second_quote"]
            and body.count(row["first_quote"]) < 2
        ):
            raise ValueError("语义重复不能把同一处原句当两处")
    if not rows:
        core_status = "unknown"
    elif any(not row["covered"] for row in rows):
        core_status = "missing"
    else:
        core_status = "covered" if complete else "unknown"
    return dict(
        core_status=core_status,
        core_coverage=rows,
        core_coverage_complete=complete,
        semantic_duplicates=duplicates,
        review_scope="full_report",
    )


def guard_content(
    before,
    after,
    *,
    before_document=None,
    after_document=None,
    before_bindings=(),
    after_bindings=(),
    core_quotes=(),
):
    def facts(document, body, bindings):
        if document is not None:
            return {
                key
                for s in document["sections"]
                for b in s["blocks"]
                for c in b["claims"]
                for key in c["fact_ids"] + c["premise_ids"]
            }
        return {
            key
            for b in bindings
            if b.get("text") and b["text"] in body
            for key in b.get("fact_ids", [])
        }

    old = facts(before_document, before, before_bindings)
    new = facts(after_document, after, after_bindings)

    def premise_groups(document):
        return [
            set(c["premise_ids"])
            for section in (document or {}).get("sections", [])
            for block in section["blocks"]
            for c in block["claims"]
            if c["premise_ids"]
        ]

    if any(
        not any(group <= candidate for candidate in premise_groups(after_document))
        for group in premise_groups(before_document)
    ):
        raise ContentRejected(
            "候选丢失分析前提覆盖或绑定关系，不能以其他段落引用相同事实替代"
        )
    if old - new:
        raise ContentRejected(
            "候选丢失原有事实或分析前提覆盖：" + ",".join(sorted(old - new))
        )
    if any(quote in before and quote not in after for quote in core_quotes):
        raise ContentRejected(
            "候选删除了已核验核心回答原句；改述需独立复审，不能靠删除制造进展"
        )
    return dict(
        facts_before=len(old),
        facts_after=len(new),
        missing_facts=[],
        core_quotes_preserved=True,
    )


def deduplicate_document(document):
    from services.report_document import validate_document

    current = copy.deepcopy(validate_document(document))
    seen = set()
    removed = []
    sections = []
    for section in current["sections"]:
        if section["role"] != "body":
            sections.append(section)
            continue
        blocks = []
        for block in section["blocks"]:
            content = dict(
                text=block["text"],
                kind=block["kind"],
                claims=[
                    {
                        k: c[k]
                        for k in ("quote", "fact_ids", "premise_ids", "source_ids")
                    }
                    for c in block["claims"]
                ],
            )
            key = json.dumps(content, sort_keys=True, ensure_ascii=False)
            if key in seen:
                removed.append(
                    dict(
                        block_id=block["block_id"],
                        reason="正文原文及事实/前提绑定逐字相同",
                    )
                )
            else:
                seen.add(key)
                blocks.append(block)
        if blocks:
            section["blocks"] = blocks
            sections.append(section)
    if removed:
        current["sections"] = sections
        current["revision"] += 1
        guard_content("", "", before_document=document, after_document=current)
        validate_document(current)
    return current, removed


def deduplicate_markdown(body):
    from services.report_quality import _required_heading_role

    parts = re.split(r"(\n\s*\n)", body)
    seen = set()
    role = None
    fenced = False
    removed = []
    for i in range(0, len(parts), 2):
        text = parts[i]
        headings = re.findall(r"(?m)^##\s+(.+)", text)
        if headings:
            role = _required_heading_role(headings[-1]) or "body"
        if "```" in text or "~~~" in text:
            fenced = (
                not fenced if text.count("```") % 2 or text.count("~~~") % 2 else fenced
            )
            continue
        if (
            role != "body"
            or fenced
            or re.search(r"(?m)^#{1,6}\s", text)
            or len(text.strip()) < 24
        ):
            continue
        if text in seen:
            removed.append(
                dict(paragraph=i // 2 + 1, quote=text, reason="正文及引用逐字相同")
            )
            parts[i] = ""
            if i > 0:
                parts[i - 1] = ""
        else:
            seen.add(text)
    return "".join(parts), removed


def protected_core(history):
    for row in reversed(history or []):
        checks = row.get("content_checks") or {}
        if checks.get("core_coverage"):
            return [r["quote"] for r in checks["core_coverage"] if r["covered"]]
    return []
