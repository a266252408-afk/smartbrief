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
                "生成结构化报告，只提供章节角色/标题、内容块和声明原文及事实短编号。规则："
                "1. sections 必须恰好包含一个 role=summary、至少一个 role=body、恰好一个 role=conclusion；"
                "block.kind 只能是 transition/fact/analysis/recommendation。"
                "2. facts 与 premises 都只能使用给定 facts 列表中的短编号（F1、F2...），"
                "不得使用 [S#]、来源ID、数字编号或自造编号；分析前提也必须来自该列表。"
                "facts 列表中的 statement 是事实原文，仅用于确定可绑定事实，不是你要返回的 quote。"
                "3. 每个 claim.quote 必须从你自己写的 block.text 中逐字复制，且在该块内只出现一次；"
                "同一 block 内多个 quote 不得重叠；禁止把 statement 或其他来源原文直接复制进 quote。"
                "4. block.text 中每个数字或定量陈述都必须落在同一 block 的某个 claim.quote 内并由其 facts/premises 绑定；"
                "块中没有数字时 claims 可为空；块中有数字却未绑定，整篇会被拒绝。不得补造数字、来源或因果关系。"
                "推荐做法：每个含数字的句子单独放在一个 kind=fact 块中，该块 text 就是这句话，"
                "claims 只有一项且 quote 与 text 完全相同；summary/analysis/recommendation 块不要写数字。"
                "5. block.text 与 claim.quote 中不得出现 F1、[S1] 等编号标记；quote 不能用省略号、改写或拼接。"
                "6. 只返回完整 ModelDocument JSON，不返回内部身份、偏移或回执。"
                "格式示例（仅示意，不要照抄内容）："
                '{"title":"单行标题","sections":['
                '{"role":"summary","title":"执行摘要","blocks":['
                '{"kind":"fact","text":"2025年全球工业机器人运行存量约500万台，同比增长9%。",'
                '"claims":[{"quote":"2025年全球工业机器人运行存量约500万台，同比增长9%。",'
                '"facts":["F1"],"premises":[]}]},'
                '{"kind":"analysis","text":"存量与新增属于不同统计口径，不能直接相加。","claims":[]}]},'
                '{"role":"body","title":"正文","blocks":[{"kind":"fact","text":"……","claims":[{"quote":"……",'
                '"facts":["F2"],"premises":[]}]}]},'
                '{"role":"conclusion","title":"结论与建议","blocks":[{"kind":"recommendation",'
                '"text":"建议按不同统计口径分别分析。","claims":[]}]}]}'
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
                + "；每个target只能出现一次，不得遗漏或新增；value为修正后的完整块，"
                "value.claims 的 quote 必须逐字出现在 value.text 中，未指定内容保持原文。"
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
