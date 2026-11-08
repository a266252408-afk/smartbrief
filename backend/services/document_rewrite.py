"""v3文档返工：程序冻结目标与版本，模型只返回内容操作。"""

import copy
import hashlib
import json
from typing import Literal, Any
from pydantic import Field, ValidationError, model_validator
from services.review_contracts import Contract, Text, parse_json
from services.report_document import (
    ModelBlock,
    ModelDocument,
    DocumentError,
    build_document,
    fact_context,
    render_document,
    validate_document,
)
from services.fact_binding import locate_bindings
from services.rewrite_patches import paragraph_table
from services.issue_identity import index_document, index_candidate
from services.rewrite_protocol import RewriteProtocolError, validate, pointer


class DocumentAction(Contract):
    target: Text
    operation: Literal["replace", "remove"]
    value: ModelBlock | None = None

    @model_validator(mode="after")
    def content_required(self):
        if (self.operation == "replace") != (self.value is not None):
            raise ValueError("replace必须有正文，remove不得携带正文")
        return self


class DocumentActions(Contract):
    actions: list[DocumentAction] = Field(min_length=1)


class ActionsEnvelope(Contract):
    actions: list[Any] = Field(min_length=1)


class ActionRejected(DocumentError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def model_payload(document, context):
    inverse = {key: alias for alias, key in context["facts"]["aliases"].items()}
    return dict(
        title=document["title"],
        sections=[
            dict(
                role=s["role"],
                title=s["title"],
                blocks=[
                    dict(
                        kind=b["kind"],
                        text=b["text"],
                        claims=[
                            dict(
                                quote=c["quote"],
                                facts=[
                                    inverse.get(k, "UNAVAILABLE") for k in c["fact_ids"]
                                ],
                                premises=[
                                    inverse.get(k, "UNAVAILABLE")
                                    for k in c["premise_ids"]
                                ],
                            )
                            for c in b["claims"]
                        ],
                    )
                    for b in s["blocks"]
                ],
            )
            for s in document["sections"]
        ],
    )


def _model_content(payload):
    value = ModelDocument.model_validate(payload).model_dump()
    for section in value["sections"]:
        for block in section["blocks"]:
            block.pop("kind")
    return value


def rewrite_context(document, evidence):
    document = validate_document(document)
    targets = {}
    for si, section in enumerate(document["sections"]):
        for block in section["blocks"]:
            targets[f"B{len(targets) + 1}"] = dict(
                section=si, block_id=block["block_id"]
            )
    return dict(
        task_id=document["task_id"],
        base_revision=document["revision"],
        document_sha256=hashlib.sha256(
            json.dumps(document, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        targets=targets,
        facts=fact_context(document["task_id"], evidence),
    )


def _restore_ids(candidate, owners):
    for section, old in zip(candidate["sections"], owners["sections"]):
        section["section_id"] = old["section_id"]
        for block, before in zip(section["blocks"], old["blocks"]):
            block["block_id"] = before["block_id"]
            prior = {
                (c["quote"], tuple(c["fact_ids"]), tuple(c["premise_ids"])): c
                for c in before["claims"]
            }
            for claim in block["claims"]:
                match = prior.get(
                    (
                        claim["quote"],
                        tuple(claim["fact_ids"]),
                        tuple(claim["premise_ids"]),
                    )
                )
                if match:
                    claim["claim_id"] = match["claim_id"]
    return candidate


def _coverage_guard(before, after):
    def facts(document):
        return {
            key
            for s in document["sections"]
            for b in s["blocks"]
            for c in b["claims"]
            for key in c["fact_ids"] + c["premise_ids"]
        }

    if len(facts(after)) < len(facts(before)):
        raise DocumentError("候选删除造成事实覆盖缺口，不能删成空壳")


def apply_actions(document, payload, context, evidence, *, allowed_targets=None):
    if context != rewrite_context(document, evidence):
        raise DocumentError("返工上下文跨任务、过期或被修改")
    validate(ActionsEnvelope, payload)
    current = copy.deepcopy(document)
    accepted, rejected, seen = [], [], set()
    for index, proposal in enumerate(payload["actions"]):
        try:
            action = DocumentAction.model_validate(proposal)
            if action.target not in context["targets"] or (
                allowed_targets is not None and action.target not in allowed_targets
            ):
                raise ActionRejected("SCOPE_RESTRICTED", "动作不属于点名目标")
            if action.target in seen:
                raise ActionRejected("DUPLICATE_TARGET", "同目标动作重复")
            target = context["targets"][action.target]
            si = target["section"]
            bi = next(
                i
                for i, b in enumerate(current["sections"][si]["blocks"])
                if b["block_id"] == target["block_id"]
            )
            value = model_payload(current, context)
            owners = copy.deepcopy(current)
            if action.operation == "remove":
                value["sections"][si]["blocks"].pop(bi)
                owners["sections"][si]["blocks"].pop(bi)
            else:
                block = action.value.model_dump()
                before_block = ModelBlock.model_validate(
                    value["sections"][si]["blocks"][bi]
                ).model_dump()
                substantive_before = {
                    k: v for k, v in before_block.items() if k != "kind"
                }
                substantive_after = {k: v for k, v in block.items() if k != "kind"}
                if substantive_before == substantive_after:
                    raise ActionRejected("NO_CHANGE", "动作没有实际修改")
                value["sections"][si]["blocks"][bi] = block
            candidate = build_document(
                value,
                document["task_id"],
                evidence,
                context["facts"],
                revision=document["revision"] + 1,
            )
            _coverage_guard(current, candidate)
            current = _restore_ids(candidate, owners)
            validate_document(current, document["task_id"])
            seen.add(action.target)
            accepted.append(
                dict(
                    target=action.target,
                    block_id=target["block_id"],
                    operation=action.operation,
                )
            )
        except (ValueError, StopIteration) as exc:
            code = (
                exc.code
                if isinstance(exc, ActionRejected)
                else "CONTRACT_REJECTED"
                if isinstance(exc, ValidationError)
                else "DOCUMENT_REJECTED"
            )
            rejected.append(
                dict(
                    index=index,
                    code=code,
                    message=str(exc),
                    proposal=proposal,
                    errors=[
                        dict(
                            path=f"/actions/{index}" + pointer(e["loc"]),
                            type=e["type"],
                            message=e["msg"],
                        )
                        for e in exc.errors(include_input=False, include_url=False)
                    ]
                    if isinstance(exc, ValidationError)
                    else [],
                )
            )
    return dict(document=current, accepted=accepted, rejected=rejected)


def _allowed_targets(document, context, findings):
    if not findings or any(f.get("scope") == "full" for f in findings):
        return set(context["targets"])
    from services.report_document import render_parts

    body = "\n\n".join(text for _, text in render_parts(document))
    rows = paragraph_table(body)
    wanted = {f.get("location", {}).get("paragraph") for f in findings}
    allowed = set()
    offset = 0
    ranges = {}
    for block_id, text in render_parts(document):
        if block_id:
            ranges[block_id] = (offset, offset + len(text))
        offset += len(text) + 2
    cursor = 0
    for row in rows:
        start = body.find(row["text"], cursor)
        end = start + len(row["text"])
        cursor = end
        if row["paragraph"] in wanted:
            for short, target in context["targets"].items():
                left, right = ranges[target["block_id"]]
                if max(start, left) < min(end, right):
                    allowed.add(short)
    return allowed


async def rewrite_document(chat, state):
    original = validate_document(state.get("report_document"), state["task_id"])
    evidence = state.get("evidence", [])
    context = rewrite_context(original, evidence)
    full = state.get("full_rewrite_required") or any(
        f.get("scope") == "full" for f in state.get("findings", [])
    )
    allowed = _allowed_targets(original, context, state.get("findings", []))
    model = model_payload(original, context)
    targets = [
        dict(
            target=key,
            value=model["sections"][t["section"]]["blocks"][
                next(
                    i
                    for i, b in enumerate(original["sections"][t["section"]]["blocks"])
                    if b["block_id"] == t["block_id"]
                )
            ],
        )
        for key, t in context["targets"].items()
        if key in allowed
    ]
    facts = [
        dict(alias=a, quote=context["facts"]["registry"][key]["quote"])
        for a, key in context["facts"]["aliases"].items()
    ]
    messages = [
        dict(
            role="system",
            content="按审核意见修正结构化报告。只修改提供的短目标，事实/前提使用给定短编号。不编造数据或来源；删除不得损失核心信息。",
        ),
        dict(
            role="user",
            content=json.dumps(
                dict(
                    topic=state.get("topic"),
                    findings=[
                        dict(problem=f.get("problem"), expected=f.get("expected"))
                        for f in state.get("findings", [])
                    ],
                    targets=targets,
                    facts=facts,
                    mode="full" if full else "actions",
                ),
                ensure_ascii=False,
            ),
        ),
    ]
    attempts = []
    current = original
    accepted, rejected = [], []
    for attempt in range(2):
        audit = dict(mode="document_full" if full else "document_actions")
        try:
            response = await chat(
                messages
                + (
                    [dict(role="user", content="规范纠正：" + attempts[-1]["message"])]
                    if attempts
                    else []
                ),
                role="rewrite",
                output_contract=ModelDocument if full else DocumentActions,
            )
            raw = response.choices[0].message.content or ""
            audit.update(
                raw_response=raw[:64000],
                raw_length=len(raw),
                raw_truncated=len(raw) > 64000,
                raw_sha256=hashlib.sha256(raw.encode()).hexdigest(),
            )
            if getattr(response.choices[0], "finish_reason", None) == "length":
                raise DocumentError("返工输出被截断")
            payload = parse_json(raw)
            if full:
                parsed = ModelDocument.model_validate(payload)
                if _model_content(parsed.model_dump()) == _model_content(model):
                    raise DocumentError("全文没有实际修改")
                candidate = build_document(
                    payload,
                    state["task_id"],
                    evidence,
                    context["facts"],
                    revision=original["revision"] + 1,
                )
                _coverage_guard(original, candidate)
                current = candidate
                accepted = [dict(operation="full")]
            else:
                result = apply_actions(
                    original, payload, context, evidence, allowed_targets=allowed
                )
                current, accepted, rejected = (
                    result["document"],
                    result["accepted"],
                    result["rejected"],
                )
                if (
                    not accepted
                    and rejected
                    and all(r["code"] == "CONTRACT_REJECTED" for r in rejected)
                ):
                    raise RewriteProtocolError(
                        "CONTRACT_REJECTED",
                        "actions规范错误",
                        [e for r in rejected for e in r["errors"]],
                    )
            audit.update(
                decision="applied" if accepted else "rejected",
                accepted=accepted,
                rejected=rejected,
            )
            attempts.append(audit)
            break
        except (ValueError, TypeError) as exc:
            audit.update(
                decision="rejected",
                code="CONTRACT_REJECTED"
                if isinstance(
                    exc, (ValidationError, json.JSONDecodeError, RewriteProtocolError)
                )
                else "DOCUMENT_REJECTED",
                message=str(exc),
            )
            attempts.append(audit)
            if isinstance(exc, RewriteProtocolError):
                audit["errors"] = exc.errors
            if not isinstance(
                exc, (ValidationError, json.JSONDecodeError, RewriteProtocolError)
            ):
                break
    body = render_document(current)
    changed = current != original
    history = copy.deepcopy(state.get("review_history") or [dict(round=0)])
    receipt = dict(
        mode="document_full" if full else "document_actions",
        changed=changed,
        applied=changed,
        accepted=accepted,
        rejected=rejected,
        attempts=attempts,
        context=dict(
            task_id=context["task_id"],
            base_revision=context["base_revision"],
            targets=context["targets"],
            aliases=context["facts"]["aliases"],
            registry_revision=context["facts"]["registry_revision"],
        ),
        program_restriction=dict(
            observed=bool(
                rejected or any(a["decision"] == "rejected" for a in attempts)
            ),
            outcome="candidate_applied" if changed else "candidate_blocked",
            quality_effect="unknown_until_review",
            codes=sorted(
                {r["code"] for r in rejected}
                | {a["code"] for a in attempts if a.get("code")}
            ),
        ),
    )
    history[-1]["rewrite"] = receipt
    if not changed:
        history[-1]["stop_reason"] = "REWRITE_NO_CHANGE"
    old_index = index_document(
        state["task_id"],
        state.get("draft_content") or render_document(original),
        state.get("content_index"),
    )
    index = index_candidate(
        state["task_id"],
        state.get("draft_content") or render_document(original),
        body,
        old_index,
        patch=not full,
    )
    claims = [
        dict(
            paragraph=1,
            text=c["quote"],
            fact_ids=list(dict.fromkeys(c["fact_ids"] + c["premise_ids"])),
            source_ids=c["source_ids"],
        )
        for s in current["sections"]
        for b in s["blocks"]
        for c in b["claims"]
    ]
    return dict(
        report_document=current,
        draft_content=body,
        content_index=index,
        review_history=history,
        claim_bindings=locate_bindings(body, claims),
        fact_registry=context["facts"]["registry"],
        passed=False,
        quality_status="rejected",
        forced_output=not changed,
    )
