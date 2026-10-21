"""显式演示样例：只导入带演示标识的历史数据，不启动真实生成。

样例来自 backend/data/demo/*.json。导入使用固定 task_id，遇到非演示任务冲突时
整批回滚，不覆盖用户数据；重复导入仅返回已导入，不重复写日志或用量。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from db import task_repository, usage_repository
from db.database import get_connection
from services.citations import check_citations
from services.evidence import Evidence, validate_sources
from services.retrieval_service import DocumentId, RetrievalRecord
from services.task_metadata import TaskMetadata
from services.task_outcome import QualityStatus
from services.usage_service import ModelUsage

DEMO_DIR = Path(__file__).resolve().parents[1] / "data" / "demo"


class DemoImportError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


class DemoPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: int = Field(ge=1, le=50)
    agent: Literal["researcher", "writer"]
    task: str = Field(min_length=1, max_length=300)
    depends_on: list[int] = Field(default_factory=list, max_length=10)


class DemoLog(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    step_id: int = Field(default=0, ge=0, le=50)
    log_type: Literal["Thought", "Action", "Observation"]
    content: str = Field(min_length=1, max_length=4000)


class DemoReviewRound(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    round: int = Field(ge=0, le=50)
    passed: StrictBool | None = None
    quality_status: QualityStatus | None = None
    comments: list[str] = Field(default_factory=list, max_length=20)


class DemoTaskPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    topic: str = Field(min_length=1, max_length=200)
    status: Literal["completed", "paused"]
    execution_status: Literal["completed", "paused"]
    quality_status: QualityStatus
    forced_output: StrictBool = False
    error_code: str | None = None
    error: str | None = None
    plan: list[DemoPlanStep] = Field(min_length=1, max_length=20)
    completed_steps: list[int] = Field(default_factory=list, max_length=20)
    current_step_index: int = Field(default=0, ge=0, le=20)
    research_data: dict = Field(default_factory=dict)
    draft_content: str = Field(default="", max_length=50000)
    final_report: str | None = Field(default=None, max_length=50000)
    review_comments: list[str] = Field(default_factory=list, max_length=20)
    review_history: list[DemoReviewRound] = Field(default_factory=list, max_length=20)
    iteration: int = Field(default=0, ge=0, le=20)
    max_iterations: int = Field(default=3, ge=1, le=20)
    passed: StrictBool | None = None
    forced_pass: StrictBool = False
    memory_hits: list[dict] = Field(default_factory=list, max_length=20)
    document_ids: list[DocumentId] = Field(default_factory=list, max_length=20)
    retrievals: list[RetrievalRecord] = Field(default_factory=list, max_length=20)
    evidence: list[Evidence] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def coherent(self):
        if self.status != self.execution_status:
            raise ValueError("演示任务执行状态不一致")
        if self.status == "completed" and not (self.final_report or "").strip():
            raise ValueError("已完成演示任务必须带报告")
        if self.status == "paused" and self.final_report is not None:
            raise ValueError("暂停演示任务不能带已完成报告")
        if len(set(self.completed_steps)) != len(self.completed_steps):
            raise ValueError("演示任务完成步骤重复")
        plan_ids = {step.id for step in self.plan}
        if len(plan_ids) != len(self.plan) or not set(self.completed_steps) <= plan_ids:
            raise ValueError("演示任务计划步骤不唯一或完成步骤越界")
        for step in self.plan:
            if any(dep not in plan_ids or dep >= step.id for dep in step.depends_on):
                raise ValueError("演示任务步骤依赖必须指向更早步骤")
        if self.current_step_index != len(self.completed_steps):
            raise ValueError("演示任务进度与完成步骤不一致")
        return self


class DemoSample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sample_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,39}$")
    sample_version: str = Field(pattern=r"^\d+\.\d+$")
    task_id: str = Field(pattern=r"^demo-[a-z0-9-]{3,48}$")
    scenario: Literal["normal", "document", "recovery"]
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=500)
    task: DemoTaskPayload
    logs: list[DemoLog] = Field(default_factory=list, max_length=200)
    usage: list[ModelUsage] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def ownership(self):
        sources = [e.model_dump() for e in self.task.evidence]
        validate_sources(self.task_id, sources)
        if any(record.task_id != self.task_id for record in self.task.retrievals):
            raise ValueError("演示检索记录跨任务")
        if any(entry.task_id != self.task_id for entry in self.usage):
            raise ValueError("演示用量记录跨任务")
        if len({entry.call_id for entry in self.usage}) != len(self.usage):
            raise ValueError("演示用量记录编号重复")
        if self.task.quality_status == "passed":
            checked = check_citations(self.task_id, self.task.final_report or "", sources)
            if not checked["valid"]:
                raise ValueError("演示报告引用不成立：" + "；".join(checked["errors"][:3]))
        return self


class DemoImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sample_ids: list[str] | None = None


class DemoImported(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sample_id: str
    task_id: str
    title: str
    description: str
    scenario: Literal["normal", "document", "recovery"]
    sample_version: str
    imported: StrictBool


class DemoImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    imported: list[DemoImported]
    skipped: list[DemoImported]


def load_samples(directory: Path | None = None) -> list[DemoSample]:
    root = Path(directory) if directory is not None else DEMO_DIR
    files = sorted(root.glob("*.json")) if root.is_dir() else []
    if not files:
        raise DemoImportError("DEMO_DATA_MISSING", "演示样例文件不可用", 503)
    samples = []
    for path in files:
        try:
            samples.append(DemoSample.model_validate(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise DemoImportError("DEMO_DATA_INVALID", f"演示样例文件无效：{path.name}", 500) from exc
    if len({s.sample_id for s in samples}) != len(samples) or len({s.task_id for s in samples}) != len(samples):
        raise DemoImportError("DEMO_DATA_INVALID", "演示样例标识或任务编号重复", 500)
    return samples


def _summary(sample: DemoSample, imported: bool) -> dict:
    return dict(sample_id=sample.sample_id, task_id=sample.task_id, title=sample.title,
                description=sample.description, scenario=sample.scenario,
                sample_version=sample.sample_version, imported=imported)


def list_samples(directory: Path | None = None) -> list[dict]:
    samples = load_samples(directory)
    with get_connection() as conn:
        rows = conn.execute("SELECT task_id FROM tasks WHERE task_id IN (%s)" % ",".join("?" * len(samples)),
                            [sample.task_id for sample in samples]).fetchall()
    existing = {row[0] for row in rows}
    return [_summary(sample, sample.task_id in existing) for sample in samples]


def is_demo_task(task: dict | None) -> bool:
    metadata = (task or {}).get("metadata") or {}
    return metadata.get("demo") is True


def _insert(conn, sample: DemoSample) -> None:
    payload = sample.task
    sources = [entry.model_dump() for entry in payload.evidence]
    checked = check_citations(sample.task_id, payload.final_report or "", sources)
    metadata = TaskMetadata(demo=True, generator_version=f"demo-{sample.sample_version}",
                            demo_sample_id=sample.sample_id, demo_sample_version=sample.sample_version)
    task_repository.create_task(sample.task_id, payload.topic, document_ids=payload.document_ids,
                                initial_status="running", metadata=metadata.model_dump(), _connection=conn)
    task_repository.update_task(
        sample.task_id, _connection=conn,
        status=payload.status, execution_status=payload.execution_status,
        quality_status=payload.quality_status, forced_output=payload.forced_output,
        error_code=payload.error_code, error=payload.error,
        plan=[step.model_dump() for step in payload.plan],
        completed_steps=payload.completed_steps, current_step_index=payload.current_step_index,
        research_data=payload.research_data, draft_content=payload.draft_content,
        final_report=payload.final_report, review_comments=payload.review_comments,
        review_history=[round_.model_dump() for round_ in payload.review_history],
        iteration=payload.iteration, max_iterations=payload.max_iterations,
        passed=payload.passed, forced_pass=payload.forced_pass, memory_hits=payload.memory_hits,
        document_ids=payload.document_ids, retrievals=[record.model_dump() for record in payload.retrievals],
        evidence=sources, citations=checked["citations"], citation_errors=checked["errors"],
    )
    for log in sample.logs:
        task_repository.append_log(sample.task_id, log.step_id, log.log_type, log.content, _connection=conn)
    for entry in sample.usage:
        usage_repository.save(entry, _connection=conn)


def import_samples(sample_ids: list[str] | None = None, directory: Path | None = None) -> dict:
    samples = load_samples(directory)
    by_id = {sample.sample_id: sample for sample in samples}
    if sample_ids is None:
        chosen = samples
    else:
        if len(set(sample_ids)) != len(sample_ids):
            raise DemoImportError("DEMO_SAMPLE_DUPLICATED", "演示样例编号不得重复")
        unknown = sorted(set(sample_ids) - set(by_id))
        if unknown:
            raise DemoImportError("DEMO_SAMPLE_NOT_FOUND", "未知演示样例：" + ", ".join(unknown))
        chosen = [by_id[sample_id] for sample_id in sample_ids]
    imported, skipped = [], []
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for sample in chosen:
            row = conn.execute("SELECT metadata FROM tasks WHERE task_id=?", (sample.task_id,)).fetchone()
            if row is not None:
                try:
                    existing = json.loads(row["metadata"] or "{}")
                except json.JSONDecodeError:
                    existing = {}
                if existing.get("demo") is True and existing.get("demo_sample_id") == sample.sample_id:
                    skipped.append(_summary(sample, True))
                    continue
                raise DemoImportError("DEMO_CONFLICT", f"任务 {sample.task_id} 已存在且不是同一样例，未覆盖用户数据", 409)
            _insert(conn, sample)
            imported.append(_summary(sample, True))
    return DemoImportResult(imported=imported, skipped=skipped).model_dump()
