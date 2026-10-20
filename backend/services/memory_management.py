"""记忆管理服务；登记状态优先，删除失败也不恢复召回资格。"""
import re
from db import memory_repository as registry
from memory import long_term_memory as memory
from rag.store import get_store, MEMORY_COLLECTION
from services.memory_policy import MemoryEntry

STATES = {"active", "unverified", "unavailable", "invalid", "deleted"}


class InvalidMemoryId(ValueError):
    pass


def _items() -> list[MemoryEntry]:
    entries = []
    for entry in registry.entries():
        if entry.status == "active" and not memory._active(entry):
            entry = registry.get(entry.memory_id)
            if entry.status == "active":
                entry = MemoryEntry.model_validate(entry.model_dump() | {"status":"unavailable", "verified":False, "invalid_reason":"source_check_unavailable"})
        entries.append(entry)
    known = {e.memory_id for e in entries}
    for row in get_store(None, MEMORY_COLLECTION).get_documents():
        meta = row["metadata"] or {}
        key = "legacy_" + row["id"]
        if meta.get("memory_id") in known or key in known:
            continue
        entries.append(MemoryEntry(memory_id=key, task_id=meta.get("task_id", ""), topic=meta.get("topic", "旧记忆"),
            quality_status="unknown", verified=False, status="unverified", report_hash=None,
            model_name=None, model_version=None, prompt_version=None, created_at=None,
            invalid_reason="legacy_unverified"))
    return sorted(entries, key=lambda e: (e.created_at or "", e.memory_id), reverse=True)


def list_memories(page: int = 1, page_size: int = 10, status: str | None = None) -> dict:
    if not 1 <= page or not 1 <= page_size <= 100 or status is not None and status not in STATES:
        raise ValueError("分页或记忆状态无效")
    entries = _items()
    counts = {state: sum(e.status == state for e in entries) for state in STATES}
    selected = [e for e in entries if status is None or e.status == status]
    return {"items":[e.model_dump() for e in selected[(page-1)*page_size:page*page_size]],
            "total":len(selected), "page":page, "page_size":page_size, "enabled":memory.is_enabled(), "counts":counts}


def delete_memory(memory_id: str) -> dict | None:
    if not re.fullmatch(r"[a-f0-9]{64}|legacy_[a-f0-9]{32}", memory_id):
        raise InvalidMemoryId("记忆ID无效")
    entry = registry.get(memory_id)
    index = get_store(None, MEMORY_COLLECTION)
    if entry is None:
        entry = next((e for e in _items() if e.memory_id == memory_id), None)
        if entry is None:
            return None
        entry = registry.reserve(entry)
    changed = entry.status != "deleted"
    # 先持久化墓碑，再调用外部索引；任何后续错误都不能让旧版本复活。
    registry.transition(memory_id, "deleted", "user_deleted")
    if memory_id.startswith("legacy_"):
        ids = [memory_id.removeprefix("legacy_")]
    else:
        ids = [row["id"] for row in index.get_documents(where={"memory_id":memory_id})]
    index.delete_by_ids(ids)
    return {"memory_id":memory_id, "deleted":changed, "status":"deleted"}
