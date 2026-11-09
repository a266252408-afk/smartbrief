"""v3 Writer：最小内容输出与有界块纠正。"""

import copy
import json
import hashlib
from pydantic import Field
from services.review_contracts import Contract, Text, parse_json
from services.report_document import (
    ModelDocument,
    ModelBlock,
    DocumentError,
    build_document,
    fact_context,
    validate_document,
)


class BlockEdit(Contract):
    target: Text
    value: ModelBlock


class BlockCorrections(Contract):
    blocks: list[BlockEdit] = Field(min_length=1)


def writer_provenance(state):
    if state.get("report_pipeline_version") != 3:
        return {}
    document = state.get("report_document")
    return dict(
        report_pipeline_version=3,
        writer_attempts=state.get("writer_attempts", []),
        report_document_revision=document["revision"] if document else None,
        report_document_sha256=hashlib.sha256(
            json.dumps(document, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if document
        else None,
    )


def _block_errors(payload, task_id, evidence, context):
    errors = []
    if not isinstance(payload, dict) or not isinstance(payload.get("sections"), list):
        return errors
    number = 0
    for si, section in enumerate(payload["sections"]):
        if not isinstance(section, dict) or not isinstance(section.get("blocks"), list):
            return []
        for bi, block in enumerate(section["blocks"]):
            number += 1
            try:
                test = dict(
                    title="块校验",
                    sections=[
                        dict(role=role, title=role, blocks=[block])
                        for role in ("summary", "body", "conclusion")
                    ],
                )
                build_document(test, task_id, evidence, context)
            except ValueError as exc:
                errors.append(
                    dict(
                        target=f"B{number}",
                        section=si,
                        block=bi,
                        value=block,
                        message=str(exc),
                    )
                )
    return errors


async def generate_document(
    chat, task_id, topic, evidence, *, previous=None, policy=None
):
    if previous is not None:
        validate_document(previous, task_id)
    context = fact_context(task_id, evidence)
    facts = [
        dict(
            alias=alias,
            statement=context["registry"][key]["quote"],
            source_id=context["registry"][key]["source_id"],
        )
        for alias, key in context["aliases"].items()
    ]
    messages = [
        dict(
            role="system",
            content=(
                "生成结构化报告，只提供章节角色/标题和分段内容。规则："
                "1. sections 必须恰好包含一个 role=summary、至少一个 role=body、恰好一个 role=conclusion；"
                "block.kind 只能是 transition/fact/analysis/recommendation。"
                "2. 每个 block 只有 segments 列表；程序按顺序拼接各段 text 作为块正文，并把该段 text 直接作为声明原文，"
                "因此不要返回 text、quote、偏移或内部身份。"
                "3. facts 与 premises 只能使用给定 facts 列表中的短编号（F1、F2...），"
                "不得使用 [S#]、来源ID、数字编号或自造编号；facts 列表中的 statement 只是事实原文，不是要返回的内容。"
                "4. 含数字的段必须在 facts 或 premises 中绑定对应事实，程序会把整段作为一条声明校验；"
                "纯分析/建议段可以不带事实，但段内不得出现数字。不得补造数字、来源或因果关系。"
                "5. segments 的 text 不得出现 F1、[S1] 等编号标记；每段是一句或一个完整片段，段与段按顺序直接拼接。"
                "6. 只返回完整 ModelDocument JSON。"
                "格式示例（仅示意，不要照抄内容）："
                '{"title":"单行标题","sections":['
                '{"role":"summary","title":"执行摘要","blocks":['
                '{"kind":"fact","segments":[{"text":"2025年全球工业机器人运行存量约500万台，同比增长9%。",'
                '"facts":["F1"],"premises":[]}]},'
                '{"kind":"analysis","segments":[{"text":"存量与新增属于不同统计口径，不能直接相加。",'
                '"facts":[],"premises":[]}]}]},'
                '{"role":"body","title":"正文","blocks":[{"kind":"fact","segments":[{"text":"……",'
                '"facts":["F2"],"premises":[]}]}]},'
                '{"role":"conclusion","title":"结论与建议","blocks":[{"kind":"recommendation",'
                '"segments":[{"text":"建议按不同统计口径分别分析。","facts":[],"premises":[]}]}]}]}'
            ),
        ),
        dict(
            role="user",
            content=json.dumps(dict(topic=topic, facts=facts), ensure_ascii=False),
        ),
    ]
    if policy:
        from services.review_policy import policy_text

        messages.append(dict(role="user", content=policy_text(policy)))
    attempts = []
    pending = []
    payload = None
    for attempt in range(2):
        local = bool(pending)
        request = (
            dict(
                targets=[
                    {k: t[k] for k in ("target", "value", "message")} for t in pending
                ],
                instruction="仅返回blocks[{target,value}]，修正指定块，未指定内容保持原文。",
            )
            if local
            else dict(
                instruction="只返回完整ModelDocument内容，不返回内部身份、偏移或回执。",
                feedback=attempts[-1].get("message") if attempts else None,
            )
        )
        if local:
            request["instruction"] = (
                f"必须返回且仅返回以下{len(pending)}个targets："
                + "、".join(t["target"] for t in pending)
                + "；每个target只能出现一次，不得遗漏或新增；value为修正后的完整块（segments，程序拼接生成正文与声明原文），"
                "未指定内容保持原文。"
            )
        audit = dict(
            mode="blocks" if local else "document",
            context={
                k: context[k] for k in ("task_id", "registry_revision", "aliases")
            },
        )
        try:
            response = await chat(
                messages
                + [dict(role="user", content=json.dumps(request, ensure_ascii=False))],
                output_contract=BlockCorrections if local else ModelDocument,
            )
            raw = response.choices[0].message.content or ""
            audit.update(
                raw_response=raw[:64000],
                raw_length=len(raw),
                raw_truncated=len(raw) > 64000,
                raw_sha256=hashlib.sha256(raw.encode()).hexdigest(),
            )
            if getattr(response.choices[0], "finish_reason", None) == "length":
                raise DocumentError("结构化报告响应被截断")
            value = parse_json(raw)
            if local:
                edits = BlockCorrections.model_validate(value).blocks
                expected = {t["target"]: t for t in pending}
                if len(edits) != len(expected) or {e.target for e in edits} != set(
                    expected
                ):
                    raise DocumentError("块纠正范围无效或重复")
                candidate = copy.deepcopy(payload)
                for edit in edits:
                    target = expected[edit.target]
                    candidate["sections"][target["section"]]["blocks"][
                        target["block"]
                    ] = edit.value.model_dump(exclude_unset=True)
                payload = candidate
            else:
                payload = value
            errors = _block_errors(payload, task_id, evidence, context)
            if errors:
                pending = errors
                raise DocumentError(
                    "局部内容待纠正：" + "；".join(t["message"] for t in errors)
                )
            document = build_document(
                payload,
                task_id,
                evidence,
                context,
                revision=previous["revision"] + 1 if previous else 1,
            )
            audit["decision"] = "applied"
            attempts.append(audit)
            return dict(
                document=document, payload=payload, attempts=attempts, applied=True
            )
        except ValueError as exc:
            audit.update(decision="rejected", message=str(exc))
            attempts.append(audit)
    if previous is not None:
        return dict(
            document=previous, payload=payload, attempts=attempts, applied=False
        )
    error = DocumentError("结构化报告生成失败，未采用旧Markdown或删除内容")
    error.attempts = attempts
    raise error
