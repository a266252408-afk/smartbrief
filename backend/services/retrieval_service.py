"""选定上传文档的确定性筛选与任务检索快照。"""
from typing import Annotated, Literal
from datetime import datetime, timezone
import math

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from services.evidence import Evidence, validate_sources

DocumentId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]


class DocumentSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    document_ids: list[DocumentId] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("文档ID不得重复")
        return self


class RetrievalOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    top_k: int = Field(default=5, ge=1, le=20)
    threshold: float = Field(default=0.35, ge=-1, le=1, allow_inf_nan=False)
    context_budget: int = Field(default=6000, ge=256, le=24000)


class RetrievalRecord(DocumentSelection):
    task_id: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=500)
    options: RetrievalOptions = Field(default_factory=RetrievalOptions)
    status: Literal["not_selected", "matched", "no_hits", "below_threshold", "budget_excluded", "documents_missing", "embedding_failed", "index_failed"]
    evidence: list[Evidence] = Field(default_factory=list)
    error_code: str | None = None

    @model_validator(mode="after")
    def ownership(self):
        if any(e.task_id != self.task_id or e.kind != "document" or e.document_id not in self.document_ids for e in self.evidence):
            raise ValueError("检索证据归属不一致")
        if (self.status == "matched") != bool(self.evidence):
            raise ValueError("检索状态与证据不一致")
        validate_sources(self.task_id, [e.model_dump() for e in self.evidence])
        return self


def retrieve_documents(query: str, document_ids: list[str], top_k: int = 5, *, task_id: str, options: RetrievalOptions | None = None) -> dict:
    """先校验与过滤索引，再调用嵌入；外部失败独立记录，不泄露异常正文。"""
    from rag import store
    record = RetrievalRecord(task_id=task_id, query=query, document_ids=document_ids,
                             options=options or RetrievalOptions(top_k=top_k), status="not_selected")
    def finish(status, error=None):
        record.status, record.error_code = status, error
        return RetrievalRecord.model_validate(record.model_dump()).model_dump()
    if not document_ids:
        return finish("not_selected")
    where = {"$and": [{"category": "upload"}, {"document_id": {"$in": document_ids}}]}
    try:
        index = store.get_store()
        available = index.get_documents(where=where)
    except Exception:
        return finish("index_failed", "DOCUMENT_INDEX_FAILED")
    if set(document_ids) - {d["metadata"].get("document_id") for d in available}:
        return finish("documents_missing", "DOCUMENTS_MISSING")
    try:
        vector = index.embeddings.embed_query(query)
    except Exception:
        return finish("embedding_failed", "DOCUMENT_EMBEDDING_FAILED")
    try:
        hits = index.similarity_search(vector, k=min(record.options.top_k, len(available)), where=where)
        # 以真实索引ID核对返回值，不信任向量适配器携带的额外元数据。
        known = {(d["metadata"].get("document_id"), d["content"]): d for d in available}
        used, size, relevant = set(), 0, False
        for doc, score in hits:
            item = known.get((doc.metadata.get("document_id"), doc.page_content))
            if item is None or item["id"] in used or not math.isfinite(score) or not -1 <= score <= 1.000001:
                continue
            if score < record.options.threshold:
                continue
            relevant = True
            text = item["content"]
            if not text.strip() or len(text) > 4000 or size + len(text) > record.options.context_budget:
                continue
            meta = item["metadata"]
            record.evidence.append(Evidence(source_id=f"S{len(record.evidence) + 1}", task_id=task_id,
                kind="document", document_id=meta["document_id"], chunk_id=item["id"],
                title=(meta.get("display_name") or meta.get("source") or "上传文档")[:500],
                excerpt=text, page=meta.get("page_number"), score=min(score, 1.0),
                retrieved_at=datetime.now(timezone.utc).isoformat()))
            used.add(item["id"])
            size += len(text)
        return finish("matched" if record.evidence else "budget_excluded" if relevant else "below_threshold" if hits else "no_hits")
    except Exception:
        record.evidence = []
        return finish("index_failed", "DOCUMENT_INDEX_FAILED")


def get_retrievals(task_id: str) -> dict | None:
    from db import task_repository
    task = task_repository.get_task(task_id)
    if task is None:
        return None
    records = [RetrievalRecord.model_validate(r).model_dump() for r in task.get("retrievals", [])]
    if any(r["task_id"] != task_id for r in records):
        raise ValueError("检索记录跨任务")
    for record in records:
        record["evidence"] = refresh_document_sources(record["evidence"])
    return {"task_id": task_id, "document_ids": task["document_ids"], "retrievals": records}


def refresh_document_sources(sources: list[dict]) -> list[dict]:
    """保留历史片段，仅刷新索引是否存在；索引故障不冒充来源删除。"""
    ids = list({s["document_id"] for s in sources if s["kind"] == "document"})
    if not ids:
        return sources
    from rag import store
    try:
        items = store.get_store().get_documents(where={"$and": [{"category": "upload"}, {"document_id": {"$in": ids}}]})
    except Exception as exc:
        raise ValueError("文档索引暂不可用") from exc
    active = {(item["metadata"].get("document_id"), item["id"]) for item in items}
    return [dict(s, source_deleted=s["source_deleted"] or (s["document_id"], s["chunk_id"]) not in active)
            if s["kind"] == "document" else s for s in sources]
