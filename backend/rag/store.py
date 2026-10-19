"""RAG 知识库门面：双 Collection 管理、预置数据、上传与检索。

两个向量集合（ChromaDB PersistentClient，目录 backend/data/chroma_db）：
- rag_collection     ：RAG 知识库（模板 category=template + 术语 category=terminology
                        + 用户上传 category=upload），供 Writer 撰写时检索术语；
- memory_collection  ：长期记忆库（历史任务 (topic, final_report)，type=report），
                       供 Planner 生成 Plan 前检索相似报告做 Few-shot。

预置数据：
- data/templates/template_*.md    5 份报告模板
- data/terminologies.txt          30 个行业术语（每行一条：术语：定义）

工程容错：所有入库/检索均容忍 chromadb / 网络 / Key 缺失等异常 —— 单点失败时
返回空结果并记录 warning，保证智能体主流程不因 RAG 子系统故障而中断。
"""
import logging
from pathlib import Path
from typing import Dict, List, Optional

from rag.chunkers import RecursiveChunker
from rag.embeddings import Embeddings, create_embeddings
from rag.loaders import Document, DocumentLoader
from rag.vectorstores import ChromaVectorStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 数据目录（可被测试覆盖）
BACKEND_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BACKEND_DIR / "data"
TEMPLATES_DIR = DATA_DIR / "templates"
TERMINOLOGIES_FILE = DATA_DIR / "terminologies.txt"
CHROMA_PERSIST_DIR = DATA_DIR / "chroma_db"
UPLOADS_DIR = DATA_DIR / "uploads"

RAG_COLLECTION = "rag_collection"
MEMORY_COLLECTION = "memory_collection"

# 术语条目数与模板份数（seed 判据）
EXPECTED_TEMPLATES = 5
EXPECTED_TERMS = 30

# ---------------------------------------------------------------- 内部状态
_embeddings: Optional[Embeddings] = None
_stores: Dict[str, ChromaVectorStore] = {}     # key=(persist_dir, collection)


def _get_embeddings() -> Embeddings:
    """嵌入模型单例（懒加载）。"""
    global _embeddings
    if _embeddings is None:
        _embeddings = create_embeddings()
    return _embeddings


def get_store(persist_dir: Optional[str] = None, collection: str = RAG_COLLECTION) -> ChromaVectorStore:
    """获取（缓存）指定集合的 ChromaVectorStore 实例。"""
    persist = persist_dir or str(CHROMA_PERSIST_DIR)
    key = f"{persist}::{collection}"
    if key not in _stores:
        _stores[key] = ChromaVectorStore(
            embeddings=_get_embeddings(),
            persist_dir=persist,
            collection_name=collection,
        )
    return _stores[key]


def _load_seed_documents(templates_dir: Optional[Path], terms_file: Optional[Path]) -> List[Document]:
    """读取预置 seed 数据：模板目录 *.md + 术语文件每行一条。

    :return: 未分块的 Document 列表（模板与术语均带 category metadata）
    """
    docs: List[Document] = []
    templates = Path(templates_dir or TEMPLATES_DIR)
    if templates.is_dir():
        for f in sorted(templates.glob("*.md")):
            text = f.read_text(encoding="utf-8").strip()
            if text:
                docs.append(Document(
                    page_content=text,
                    metadata={
                        "category": "template",
                        "source": f.name,
                        "file_type": "markdown",
                        "title": f.stem,
                    },
                ))
    terms = Path(terms_file or TERMINOLOGIES_FILE)
    if terms.is_file():
        for line in terms.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            term = line.split("：", 1)[0].split(":", 1)[0].strip()
            docs.append(Document(
                page_content=line,
                metadata={
                    "category": "terminology",
                    "source": terms.name,
                    "file_type": "txt",
                    "term": term,
                },
            ))
    return docs


def seed_rag_knowledge(persist_dir: Optional[str] = None,
                       templates_dir: Optional[Path] = None,
                       terms_file: Optional[Path] = None) -> dict:
    """预置知识库数据（幂等，已就绪则跳过；首次/不足时补齐并入库）。

    供 main.py 生命周期启动时调用一次；测试可通过显式传参隔离。
    :return: {"templates": n, "terms": m, "skipped": bool}
    """
    try:
        store = get_store(persist_dir, RAG_COLLECTION)
        t_count = store.count({"category": "template"})
        g_count = store.count({"category": "terminology"})
        if t_count >= EXPECTED_TEMPLATES and g_count >= EXPECTED_TERMS:
            return {"templates": t_count, "terms": g_count, "skipped": True}
        # 补齐缺失部分：逐类别统计现有 source 集，只入库尚未预置的
        seed_docs = _load_seed_documents(templates_dir, terms_file)
        if seed_docs:
            chunks = RecursiveChunker().chunk(seed_docs)
            store.add_documents(chunks)     # md5 稳定 ID → 天然去重
        return {
            "templates": store.count({"category": "template"}),
            "terms": store.count({"category": "terminology"}),
            "skipped": False,
        }
    except Exception as exc:                 # pragma: no cover - 真实异常路径（网络/依赖）
        logger.warning("RAG 预置数据初始化失败（不影响主流程）：%s", exc)
        return {"templates": 0, "terms": 0, "skipped": False, "error": str(exc)}


def ingest_file(file_bytes: bytes, filename: str, category: str = "upload",
                persist_dir: Optional[str] = None) -> dict:
    """把上传文件写入知识库：存盘 → 分格式加载 → 递归分块 → 入库。

    :return: {"source": filename, "added": chunk数, "chunks": n}
    """
    upload_dir = Path(persist_dir and str(Path(persist_dir).parent / "uploads") or UPLOADS_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / filename
    target.write_bytes(file_bytes)

    docs = DocumentLoader().load(str(target))
    chunks = RecursiveChunker().chunk(docs)
    for chunk in chunks:
        chunk.metadata["category"] = category
        chunk.metadata["source"] = filename          # 以文件名作为用户可读 source（覆盖绝对路径）
        chunk.metadata["uploaded_name"] = filename
    store = get_store(persist_dir, RAG_COLLECTION)
    added = store.add_documents(chunks)
    return {"source": filename, "category": category, "added": len(added), "chunks": len(chunks)}


def list_documents(persist_dir: Optional[str] = None) -> List[dict]:
    """文档列表：按 (category, source) 聚合 chunk，返回每个"文件"的概况。"""
    store = get_store(persist_dir, RAG_COLLECTION)
    items = store.get_documents()
    grouped: dict = {}
    for item in items:
        meta = item["metadata"] or {}
        key = (meta.get("category", ""), meta.get("source", ""))
        entry = grouped.setdefault(key, {
            "category": meta.get("category", ""),
            "source": meta.get("source", ""),
            "file_type": meta.get("file_type", ""),
            "chunk_count": 0,
            "snippet": "",
        })
        entry["chunk_count"] += 1
        if not entry["snippet"]:
            entry["snippet"] = item["content"][:120]
    return sorted(grouped.values(), key=lambda e: (e["category"], e["source"]))


def get_document_chunks(source: str, category: str,
                        persist_dir: Optional[str] = None) -> List[dict]:
    """获取指定 (category, source) 的全部 chunk（文档详情）。"""
    store = get_store(persist_dir, RAG_COLLECTION)
    # ChromaDB 多条件过滤需用 $and 语法
    items = store.get_documents(
        where={"$and": [{"category": category}, {"source": source}]}
    )
    return sorted(
        [
            {
                "id": item["id"],
                "content": item["content"],
                "metadata": item["metadata"] or {},
            }
            for item in items
        ],
        key=lambda x: x["content"][:80],
    )


def delete_document(source: str, category: str,
                    persist_dir: Optional[str] = None) -> int:
    """删除指定 (category, source) 的全部 chunk（文档详情）。

    :return: 删除的 chunk 数量
    """
    store = get_store(persist_dir, RAG_COLLECTION)
    items = store.get_documents(
        where={"$and": [{"category": category}, {"source": source}]}
    )
    ids = [item["id"] for item in items]
    if ids:
        store.delete_by_ids(ids)
    return len(ids)


def search_rag(query: str, k: int = 5, category: Optional[str] = None,
               persist_dir: Optional[str] = None) -> List[dict]:
    """RAG 语义检索（知识库管理页 /knowledge/search 的数据源）。"""
    try:
        store = get_store(persist_dir, RAG_COLLECTION)
        q_emb = store.embeddings.embed_query(query)
        where = {"category": category} if category else None
        hits = store.similarity_search(q_emb, k=k, where=where)
        return [
            {
                "content": doc.page_content,
                "source": doc.metadata.get("source", ""),
                "category": doc.metadata.get("category", ""),
                "term": doc.metadata.get("term", ""),
                "score": round(score, 4),
            }
            for doc, score in hits
        ]
    except Exception as exc:                 # pragma: no cover - 真实异常路径
        logger.warning("RAG 检索失败：%s", exc)
        return []


def search_rag_terms(query: str, k: int = 3, threshold: float = 0.35,
                     persist_dir: Optional[str] = None) -> List[str]:
    """Writer 专用：检索与当前章节最相关的术语定义（默认只取 terminology 类）。

    :return: 术语定义文本列表（"术语：定义"，供拼进 prompt）
    """
    hits = search_rag(query, k=k, category="terminology", persist_dir=persist_dir)
    return [h["content"] for h in hits if h.get("score", 0) >= threshold]
