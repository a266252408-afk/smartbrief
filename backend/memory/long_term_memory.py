"""质量准入的长期记忆；SQLite登记为权威，向量只负责相似结构检索。"""
import hashlib
import logging
import math
import os
from datetime import datetime, timezone
from typing import List, Optional

from rag.store import MEMORY_COLLECTION, get_store
from db import memory_repository as registry
from services.memory_policy import MemoryEntry, PROMPT_VERSION, digest_report, eligibility

logger = logging.getLogger(__name__)
_REPORT_LIMIT = 6000
_SNIPPET_LIMIT = 200
_MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "true").lower() not in {"0", "false", "off", "no"}


def is_enabled() -> bool:
    return _MEMORY_ENABLED


def set_enabled(enabled: bool) -> None:
    global _MEMORY_ENABLED
    _MEMORY_ENABLED = enabled


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def remember(topic: str, final_report: str, task_id: Optional[str] = None,
             persist_dir: Optional[str] = None) -> bool:
    """只接收已落库的合格原报告；稳定版本ID使崩溃重试幂等。"""
    if not _MEMORY_ENABLED or not task_id or not (final_report or "").strip():
        return False
    try:
        task, reason = eligibility(task_id)
        if reason or task["topic"] != topic or task["final_report"] != final_report:
            return False
        report_hash = digest_report(final_report)
        memory_id = hashlib.sha256(f"{task_id}\0{report_hash}".encode()).hexdigest()
        text = f"主题：{topic}\n\n{final_report[:_REPORT_LIMIT]}"
        from llm.client import get_model
        entry = registry.reserve(MemoryEntry(memory_id=memory_id, task_id=task_id, topic=topic,
            quality_status="passed", verified=False, status="unavailable", report_hash=report_hash,
            index_hash=digest_report(text),
            model_name=get_model(), model_version=None, prompt_version=PROMPT_VERSION, created_at=_now()))
        if entry.status in {"active", "deleted", "invalid"}:
            return False
        index = get_store(persist_dir, MEMORY_COLLECTION)
        from rag.loaders import Document
        existing = index.get_documents(where={"memory_id": memory_id})
        if any(item["content"] != text or item["metadata"].get("task_id") != task_id or item["metadata"].get("report_hash") != report_hash for item in existing):
            registry.transition(memory_id, "invalid", "index_conflict")
            return False
        if not existing:
            index.add_documents([Document(page_content=text,
                metadata={"type":"report", "document_id":memory_id, "memory_id":memory_id,
                          "task_id":task_id, "topic":topic, "report_hash":report_hash,
                          "verified":True, "prompt_version":PROMPT_VERSION, "created_at":entry.created_at})])
        # 索引调用期间可能发生原报告变更；重新核对后才完成准入。
        _, reason = eligibility(task_id, report_hash)
        if reason:
            if reason != "source_check_unavailable":
                registry.transition(memory_id, "invalid", reason)
            return False
        return registry.transition(memory_id, "active")
    except Exception:
        logger.warning("长期记忆写入不可用，报告结果保留")
        return False


def _extract_structure(text: str) -> List[str]:
    return [line.strip()[:120] for line in (text or "").splitlines() if line.strip().startswith("#")][:12]


def _active(entry: MemoryEntry) -> bool:
    current = registry.get(entry.memory_id)
    if current is None or current.status != "active" or not current.verified:
        return False
    _, reason = eligibility(current.task_id, current.report_hash)
    if reason:
        if reason != "source_check_unavailable":
            registry.transition(current.memory_id, "invalid", reason)
        return False
    return True


def recall(topic: str, k: int = 3, persist_dir: Optional[str] = None) -> List[dict]:
    """仅可信登记集合可召回；旧向量不因字段看似成功而取得资格。"""
    if not _MEMORY_ENABLED:
        return []
    try:
        active = {e.memory_id:e for e in registry.entries() if e.status == "active" and _active(e)}
        if not active:
            return []
        index = get_store(persist_dir, MEMORY_COLLECTION)
        where = {"$and":[{"verified":True}, {"memory_id":{"$in":list(active)}}]}
        count = index.count(where)
        if not count:
            return []
        hits = index.similarity_search(index.embeddings.embed_query(topic), k=min(k,count), where=where)
        results, seen = [], set()
        for doc, score in hits:
            meta = doc.metadata or {}
            entry = active.get(meta.get("memory_id"))
            if entry is None or entry.memory_id in seen or meta.get("task_id") != entry.task_id or meta.get("report_hash") != entry.report_hash or digest_report(doc.page_content) != entry.index_hash or not math.isfinite(score) or not _active(entry):
                continue
            seen.add(entry.memory_id)
            results.append({"topic":entry.topic, "structure":_extract_structure(doc.page_content),
                "snippet":doc.page_content[:_SNIPPET_LIMIT], "score":round(score,4), "task_id":entry.task_id,
                "created_at":entry.created_at, "memory_id":entry.memory_id, "verified":True,
                "quality_status":"passed", "prompt_version":entry.prompt_version})
        return results
    except Exception:
        logger.warning("长期记忆检索不可用，跳过结构参考")
        return []


def search_memory(query: str, k: int = 5, persist_dir: Optional[str] = None) -> List[dict]:
    return recall(query, k=k, persist_dir=persist_dir)
