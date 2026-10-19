"""长期记忆：历史任务报告入库与相似检索（ChromaDB memory_collection）。

- remember(topic, final_report)：任务成功后把 (topic, report) 写入 memory_collection；
- recall(topic)：Planner 生成 Plan 前检索与当前主题最相似的历史报告（Few-shot）；
- search_memory(q, k)：`GET /api/v1/memory/search` 内部检索。

容错设计：记忆子系统任何异常（依赖缺失 / 网络 / Key 缺失）都被吞掉并记 warning，
绝不阻断主流程 —— 没有历史记忆时召回空列表即可，任务照常执行。
"""
import logging
import os
from datetime import datetime
from typing import List, Optional

from rag.store import MEMORY_COLLECTION, get_store

logger = logging.getLogger(__name__)

# 单条报告最长入库字符数（bge-m3 有 8192 token 上限，截断以保安全）
_REPORT_LIMIT = 6000
_SNIPPET_LIMIT = 200

# 长期记忆总开关（可通过 .env 或 /memory/toggle 关闭）
_MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "true").lower() not in {"0", "false", "off", "no"}


def is_enabled() -> bool:
    """长期记忆是否启用。"""
    return _MEMORY_ENABLED


def set_enabled(enabled: bool) -> None:
    """设置长期记忆开关状态（运行时切换）。"""
    global _MEMORY_ENABLED
    _MEMORY_ENABLED = enabled
    logger.info("长期记忆开关已设置为 %s", enabled)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def remember(topic: str, final_report: str, task_id: Optional[str] = None,
             persist_dir: Optional[str] = None) -> bool:
    """把一次成功任务存入长期记忆（幂等：md5 稳定 ID 去重）。

    :return: True 表示新写入；False 表示重复（或失败/为空跳过）
    """
    if not _MEMORY_ENABLED:
        return False
    report = (final_report or "").strip()
    if not report:
        return False
    try:
        store = get_store(persist_dir, MEMORY_COLLECTION)
        from rag.loaders import Document

        doc = Document(
            page_content=f"主题：{topic}\n\n{report[:_REPORT_LIMIT]}",
            metadata={
                "type": "report",
                "topic": topic,
                "task_id": task_id or "",
                "created_at": _now(),
            },
        )
        added = store.add_documents([doc])
        if added:
            logger.info("长期记忆已写入：topic=%s task_id=%s", topic, task_id)
        return bool(added)
    except Exception as exc:  # pragma: no cover - 真实异常路径（网络/依赖）
        logger.warning("长期记忆写入失败（不影响主流程）：%s", exc)
        return False


def _extract_structure(text: str) -> List[str]:
    """从报告文本提取章节标题（Markdown 标题行，供 Planner Few-shot）。"""
    headings = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            headings.append(stripped)
    return headings[:12]


def recall(topic: str, k: int = 3, persist_dir: Optional[str] = None) -> List[dict]:
    """检索与主题最相似的历史报告（供 Planner Few-shot）。

    :return: [{"topic", "structure", "snippet", "score", "task_id", "created_at"}]，无命中返回 []
    """
    if not _MEMORY_ENABLED:
        return []
    try:
        store = get_store(persist_dir, MEMORY_COLLECTION)
        if store.count({"type": "report"}) == 0:
            return []                              # 首次运行无历史记忆
        q_emb = store.embeddings.embed_query(topic)
        hits = store.similarity_search(q_emb, k=k, where={"type": "report"})
        results = []
        for doc, score in hits:
            meta = doc.metadata or {}
            results.append({
                "topic": meta.get("topic", ""),
                "structure": _extract_structure(doc.page_content),
                "snippet": doc.page_content[:_SNIPPET_LIMIT],
                "score": round(score, 4),
                "task_id": meta.get("task_id", ""),
                "created_at": meta.get("created_at", ""),
            })
        return results
    except Exception as exc:  # pragma: no cover - 真实异常路径
        logger.warning("长期记忆检索失败：%s", exc)
        return []


def search_memory(query: str, k: int = 5, persist_dir: Optional[str] = None) -> List[dict]:
    """长期记忆检索（/api/v1/memory/search 端点直接调用，语义与 recall 相同）。"""
    return recall(query, k=k, persist_dir=persist_dir)
