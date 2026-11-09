"""确定性修复分流与完整目标修改包。"""

from typing import Literal
from pydantic import Field
from services.review_contracts import Contract, Text


class RepairTarget(Contract):
    finding_id: Text
    kind: Literal["claim", "structure", "evidence", "review", "dispute", "protocol"]
    report_quote: str
    paragraph: int = Field(ge=0)
    problem: str
    expected: str
    sources: list[dict]


class RepairPlan(Contract):
    action: Literal["rewrite", "research", "stop"]
    reason: Text
    scope: Literal["paragraph", "full", "none"]
    context_key: Text
    targets: list[RepairTarget]
    preserve: list[dict]


def plan_repair(state):
    import hashlib
    import json
    from services.rewrite_patches import paragraph_table
    from services.review_rules import is_confirmed_blocker, material_pending
    from services.report_quality import needs_research
    from services.citations import reference_ids
    from services.issue_identity import identity_key

    body = state.get("draft_content") or ""
    paragraphs = paragraph_table(body)
    findings = [
        f
        for f in state.get("findings", [])
        if f.get("decision") != "dismissed"
        and (is_confirmed_blocker(f) or material_pending(f))
    ]
    targets = []
    wanted = set()
    for finding in findings:
        n = (finding.get("location") or {}).get("paragraph", 0)
        n = n if type(n) is int and 1 <= n <= len(paragraphs) else 0
        quote = paragraphs[n - 1]["text"] if n else ""
        wanted.add(n)
        rule = finding.get("rule") or finding.get("kind")
        kind = (
            "dispute"
            if finding.get("decision") in {"suspected", "unresolved"}
            and material_pending(finding)
            else "protocol"
            if rule == "rewrite_protocol"
            else "structure"
            if rule in {"required_structure", "report_assembly"}
            else "evidence"
            if rule in {"core_evidence_gap", "evidence_gap"}
            else "claim"
        )
        ids = set(
            finding.get("source_ids") or finding.get("cited") or []
        ) | reference_ids(quote)
        sid = (finding.get("evidence") or {}).get("source_id")
        if sid:
            ids.add(sid)
        sources = [
            s
            for s in state.get("evidence", [])
            if s.get("source_id") in ids
            and not s.get("source_deleted")
            and s.get("task_id") == state.get("task_id")
        ]
        targets.append(
            RepairTarget(
                finding_id=finding["finding_id"],
                kind=kind,
                report_quote=quote,
                paragraph=n,
                problem=finding.get("problem", ""),
                expected=finding.get("expected", ""),
                sources=sources,
            ).model_dump()
        )
    diagnostics = state.get("review_diagnostics") or (
        (state.get("review_history") or [{}])[-1].get("review_diagnostics")
    )
    if diagnostics:
        targets.append(
            RepairTarget(
                finding_id="REVIEW-DIAGNOSTIC",
                kind="review",
                report_quote="",
                paragraph=0,
                problem="审核响应不可用",
                expected="仅纠正审核响应，不据此改写正文",
                sources=[],
            ).model_dump()
        )
    key_data = [
        state.get("task_id"),
        body,
        state.get("report_document"),
        sorted(state.get("evidence", []), key=lambda s: s.get("source_id", "")),
        state.get("review_policy"),
        sorted(identity_key(f, include_target=False) for f in findings),
        sorted((t["paragraph"], t["problem"], t["expected"]) for t in targets),
    ]
    key = hashlib.sha256(
        json.dumps(key_data, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    full = any(
        f.get("scope") == "full"
        and is_confirmed_blocker(f)
        and (
            f.get("rule") in {"required_structure", "report_assembly"}
            or f.get("rule") == "reviewer"
            and f.get("evidence", {}).get("kind") == "report"
        )
        for f in findings
    )
    action, reason = "rewrite", "CONFIRMED_TARGETS"
    history = state.get("review_history") or []
    used = sum(bool(h.get("rewrite", {}).get("attempts")) for h in history)
    used = max(used, max(0, state.get("iteration", 0) - 1))
    attempted = any(
        h.get("repair_plan", {}).get("context_key") == key
        and h.get("rewrite", {}).get("attempts")
        for h in history
    )
    if state.get("passed") is True or state.get("quality_status") == "passed":
        action, reason = "stop", "QUALITY_PASSED"
    elif (
        state.get("quality_status") in {"unavailable", "not_run"}
        or state.get("execution_status") == "failed"
    ):
        action, reason = "stop", "REVIEW_UNAVAILABLE"
    elif used >= 2:
        action, reason = "stop", "REWRITE_LIMIT"
    elif attempted:
        action, reason = "stop", "REPAIR_NO_PROGRESS"
    elif any(t["kind"] == "dispute" for t in targets):
        action, reason = "stop", "DISPUTE_UNRESOLVED"
    elif needs_research(state) or any(t["kind"] == "evidence" for t in targets):
        action, reason = (
            ("stop", "RESEARCH_LIMIT")
            if state.get("research_revision_count", 0) >= 1
            else ("research", "EVIDENCE_GAP")
        )
    elif not state.get("evidence") and state.get("research_revision_count", 0) >= 1:
        action, reason = "stop", "RESEARCH_LIMIT"
    elif not targets:
        action, reason = "stop", "NO_ACTIONABLE_TARGETS"
    preserve = [
        {"paragraph": p["paragraph"], "text": p["text"]}
        for p in paragraphs
        if p["paragraph"] not in wanted
    ]
    return RepairPlan(
        action=action,
        reason=reason,
        scope=("full" if full else "paragraph") if action == "rewrite" else "none",
        context_key=key,
        targets=targets,
        preserve=preserve,
    ).model_dump()


def stop_repair(state, reason):
    """保留最佳正文，终止不伪造质量通过。"""
    import copy

    history = copy.deepcopy(state.get("review_history") or [dict(round=0)])
    from services.review_rules import is_confirmed_blocker

    quality = (
        "rejected"
        if state.get("quality_status") == "rejected"
        or any(is_confirmed_blocker(f) for f in state.get("findings", []))
        else "unavailable"
    )
    history[-1].update(stop_reason=reason, quality_status=quality)
    return dict(
        draft_content=state.get("draft_content", ""),
        review_history=history,
        passed=False,
        forced_output=True,
        forced_pass=quality == "rejected",
        quality_status=quality,
        error_code=reason,
        react_logs=state.get("react_logs", []),
    )


def model_repair_package(plan):
    """模型只接收待改原句和资料，不接收内部指纹或验收回执。"""
    if not plan:
        return None
    return dict(
        scope=plan["scope"],
        targets=[
            dict(
                kind=t["kind"],
                paragraph=t["paragraph"],
                report_quote=t["report_quote"],
                problem=t["problem"],
                expected=t["expected"],
                sources=[
                    {k: s.get(k, "") for k in ("source_id", "title", "excerpt")}
                    for s in t["sources"]
                ],
            )
            for t in plan["targets"]
        ],
        preserve=plan["preserve"],
    )
