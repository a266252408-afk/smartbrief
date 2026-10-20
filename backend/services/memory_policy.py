"""长期记忆准入与生命周期契约，质量判断来自持久化任务。"""
from typing import Literal
import hashlib
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

PROMPT_VERSION = "smartbrief-report-T06-v1"


class MemoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    memory_id: str = Field(min_length=1)
    task_id: str
    topic: str
    quality_status: Literal["passed", "unknown"]
    verified: StrictBool
    status: Literal["active", "unverified", "unavailable", "invalid", "deleted"]
    report_hash: str | None
    index_hash: str | None = None
    model_name: str | None
    model_version: str | None = None
    prompt_version: str | None
    created_at: str | None
    invalid_reason: str | None = None

    @model_validator(mode="after")
    def consistency(self):
        if self.verified != (self.status == "active"):
            raise ValueError("记忆状态与验证标志不一致")
        if self.verified and (self.quality_status != "passed" or not self.report_hash or not self.index_hash or not self.prompt_version or not self.task_id):
            raise ValueError("可信记忆缺少质量或版本凭据")
        return self


def eligibility(task_id: str, report_hash: str | None = None) -> tuple[dict | None, str | None]:
    from db import task_repository
    from services.citations import check_citations
    from services.evidence import validate_sources
    from services.retrieval_service import refresh_document_sources
    task = task_repository.get_task(task_id)
    if task is None:
        return None, "task_missing"
    if task["execution_status"] != "completed":
        return task, "execution_not_completed"
    if task["quality_status"] != "passed":
        return task, "quality_not_passed"
    if task["forced_output"]:
        return task, "forced_output"
    report = task.get("final_report") or ""
    if not report.strip():
        return task, "report_empty"
    if report_hash is not None and report_hash != digest_report(report):
        return task, "report_changed"
    try:
        sources = validate_sources(task_id, task.get("evidence", []))
    except ValueError:
        return task, "sources_invalid"
    try:
        sources = refresh_document_sources(sources)
    except ValueError:
        return task, "source_check_unavailable"
    if not check_citations(task_id, report, sources)["valid"]:
        return task, "sources_invalid"
    return task, None


def digest_report(report: str) -> str:
    return hashlib.sha256(report.encode()).hexdigest()
