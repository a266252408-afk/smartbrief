"""审核发现缺少资料时，先补研究再改写；不重跑计划与已有章节。"""
import asyncio
import time

from agents.react_researcher import react_researcher_run
from agents.scheduler import AGENT_STEP_TIMEOUT_SECONDS, STEP_DEADLINE_MARGIN_SECONDS
from services.evidence import normalize_evidence
from services.event_logs import EventLogs
from services.usage_service import usage_scope


@usage_scope("researcher", state=True)
async def research_revision_node(state: dict) -> dict:
    count = state.get("research_revision_count", 0) + 1
    logs = EventLogs(state.get("react_logs", []))
    logs.append({"type": "Thought", "step_id": 0,
                 "content": f"开始补充研究（第{count}次，最多1次），按审核意见补齐证据"})
    task = (f"研究主题：{state['topic']}\n仅围绕本主题补充行业数据与相关来源，不能更换行业。\n"
            "待解决的审核意见：\n" + "\n".join(state.get("review_comments", [])))
    partial = []
    deadline = time.monotonic() + AGENT_STEP_TIMEOUT_SECONDS - STEP_DEADLINE_MARGIN_SECONDS
    try:
        result = await asyncio.wait_for(
            react_researcher_run(task, 0, logs, observations=partial, deadline=deadline),
            timeout=AGENT_STEP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        result = {"observations": partial, "skipped": True, "error": "补充研究超时"}
        logs.append({"type": "Observation", "step_id": 0,
                     "content": f"补充研究超时，保留{len(partial)}轮结果；不足部分仍需标注待补充"})
    research_data = dict(state.get("research_data", {}))
    research_data[f"review_research_{count}"] = result
    evidence = normalize_evidence(state.get("task_id", ""), research_data, state.get("evidence", []))
    added = len(evidence) - len(state.get("evidence", []))
    logs.append({"type": "Observation", "step_id": 0,
                 "content": f"补充研究结束，新增{added}条来源；是否支持声明仍需审核"})
    return {"research_revision_count": count, "research_data": research_data,
            "evidence": evidence, "react_logs": logs}
