"""RAG 模块测试（离线：FakeEmbeddings 替代真实嵌入调用）。

覆盖 rag 包四个层次：
- loaders：按扩展名分发加载（txt / md / docx / pdf / xlsx）
- chunkers：RecursiveChunker 递归分块（长文本 / 重叠兜底）
- vectorstores：ChromaVectorStore 增删查检（md5 稳定 ID、cosine 检索）
- store 门面：seed 幂等、ingest_file、list_documents、search_rag / search_rag_terms

conftest 的 autouse fixture 已把数据目录/持久化目录隔离到 tmp_path，
并把 EMBEDDING_API_KEY 置空 —— 本文件统一通过 rag_store._embeddings 注入假嵌入。
"""
import hashlib

import pytest

from rag import store as rag_store
from rag.chunkers import RecursiveChunker
from rag.loaders import Document, DocumentLoader
from rag.vectorstores import ChromaVectorStore

TMPL_DIR = "templates"
TERMS_FILE = "terminologies.txt"


class FakeEmbeddings:
    """确定性假嵌入：文本 md5 前 8 字节归一化 → 8 维向量（余弦距离可用）。"""

    dim = 8

    def _vec(self, text: str) -> list:
        digest = hashlib.md5(text.encode("utf-8")).digest()
        return [b / 255.0 for b in digest[: self.dim]]

    def embed_documents(self, texts: list) -> list:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list:
        return self._vec(text)


@pytest.fixture(autouse=True)
def _fake_embeddings():
    """每个 RAG 测试注入假嵌入，隔离真实网络调用。"""
    rag_store._embeddings = FakeEmbeddings()
    yield
    rag_store._stores.clear()
    rag_store._embeddings = None


def _write_seed(tmp_path, n_templates=5, n_terms=30):
    """在 tmp_path 下构造 seed 数据目录，返回 (templates_dir, terms_file)。"""
    templates = tmp_path / TMPL_DIR
    templates.mkdir(parents=True, exist_ok=True)
    for i in range(n_templates):
        (templates / f"template_{i}.md").write_text(
            f"# 模板 {i}\n\n## 市场规模\n\n分析 {i} 的市场规模数据。\n\n## 竞争格局\n\n头部厂商盘点。",
            encoding="utf-8",
        )
    terms = tmp_path / TERMS_FILE
    lines = [f"术语{i}：定义内容 {i}" for i in range(n_terms)]
    terms.write_text("\n".join(lines), encoding="utf-8")
    return templates, terms


# ---------------------------------------------------------------- loaders


def test_text_and_markdown_loader(tmp_path):
    f1 = tmp_path / "a.txt"
    f1.write_text("纯文本内容", encoding="utf-8")
    f2 = tmp_path / "b.md"
    f2.write_text("# 标题\n正文", encoding="utf-8")
    assert DocumentLoader().load(str(f1))[0].page_content == "纯文本内容"
    md = DocumentLoader().load(str(f2))
    assert md[0].metadata["file_type"] == "markdown"
    assert "# 标题" in md[0].page_content


def test_loader_unsupported_extension(tmp_path):
    bad = tmp_path / "x.xyz"
    bad.write_text("hi", encoding="utf-8")
    with pytest.raises(ValueError, match="不支持的文件类型"):
        DocumentLoader().load(str(bad))


# ---------------------------------------------------------------- chunkers


def test_recursive_chunker_short_text_no_split():
    chunks = RecursiveChunker(chunk_size=512).chunk([Document(page_content="短文本")])
    assert len(chunks) == 1
    assert chunks[0].page_content == "短文本"
    assert chunks[0].metadata["chunk_index"] == 0


def test_recursive_chunker_splits_long_text():
    text = "。".join([f"这是第{i}段完整句子内容，包含若干逗号。内容丰富。" for i in range(60)])
    chunks = RecursiveChunker(chunk_size=100, chunk_overlap=10).chunk([Document(page_content=text)])
    assert len(chunks) > 1
    assert all(c.page_content for c in chunks)
    # 分块保留元信息
    assert chunks[0].metadata["chunk_index"] == 0


def test_recursive_chunker_char_fallback():
    """无任何标点/空格的长文本 → 按字符兜底切分。"""
    text = "字" * 500
    chunks = RecursiveChunker(chunk_size=100, chunk_overlap=10).chunk([Document(page_content=text)])
    assert len(chunks) >= 5
    assert all(len(c.page_content) <= 100 for c in chunks)


# ---------------------------------------------------------------- vectorstores


def test_vectorstore_add_search_count_delete(tmp_path):
    store = ChromaVectorStore(embeddings=FakeEmbeddings(), persist_dir=str(tmp_path))
    added = store.add_documents([
        Document(page_content="苹果公司发布新款手机", metadata={"source": "a.md"}),
        Document(page_content="碳酸锂价格持续上涨", metadata={"source": "b.md"}),
    ])
    assert len(added) == 2
    assert store.count() == 2
    # 重复入库（md5 稳定 ID）不产生新记录
    store.add_documents([Document(page_content="苹果公司发布新款手机", metadata={"source": "a.md"})])
    assert store.count() == 2
    # 余弦检索：与库中完全相同文本的查询必然命中且相似度≈1（Fake 嵌入确定性）
    hits = store.similarity_search(FakeEmbeddings().embed_query("苹果公司发布新款手机"), k=1)
    assert hits[0][0].page_content == "苹果公司发布新款手机"
    assert hits[0][1] > 0.99
    # get/delete/count(where)
    items = store.get_documents({"source": "a.md"})
    assert len(items) == 1
    store.delete_by_ids([items[0]["id"]])
    assert store.count({"source": "a.md"}) == 0
    assert store.count() == 1


# ---------------------------------------------------------------- store 门面


def test_seed_rag_knowledge_idempotent(tmp_path):
    templates, terms = _write_seed(tmp_path, n_templates=5, n_terms=30)
    persist = str(tmp_path / "chroma")
    first = rag_store.seed_rag_knowledge(persist, templates, terms)
    assert first["skipped"] is False
    assert first["templates"] >= 5
    assert first["terms"] >= 30
    # 幂等：数据已就绪 → 第二次直接跳过
    second = rag_store.seed_rag_knowledge(persist, templates, terms)
    assert second["skipped"] is True
    assert rag_store.get_store(persist).count({"category": "template"}) >= 5


def test_ingest_file_and_list_documents(tmp_path):
    persist = str(tmp_path / "chroma")
    content = "# 上传的参考文档\n\n行业报告关键结论，用于辅助撰写。".encode("utf-8")
    result = rag_store.ingest_file(content, "upload.md", "upload", persist)
    assert result["source"] == "upload.md"
    assert result["added"] >= 1
    docs = rag_store.list_documents(persist)
    upload_entries = [d for d in docs if d["category"] == "upload" and d["source"] == "upload.md"]
    assert upload_entries and upload_entries[0]["chunk_count"] >= 1


def test_search_rag_terms_threshold(tmp_path):
    templates, terms = _write_seed(tmp_path, n_templates=1, n_terms=5)
    persist = str(tmp_path / "chroma")
    rag_store.seed_rag_knowledge(persist, templates, terms)
    # 假嵌入检索必然命中某条术语，且过滤低分
    hits = rag_store.search_rag_terms("术语1 的定义是什么", k=3, threshold=0.0, persist_dir=persist)
    assert isinstance(hits, list) and len(hits) > 0
    assert all("：" in h or ":" in h for h in hits)


def test_rag_facade_tolerates_embedding_failure(tmp_path, monkeypatch):
    """嵌入不可用时 facade 返回空列表而非抛异常（不影响主流程）。"""
    monkeypatch.setattr(rag_store, "_embeddings", None)
    assert rag_store.search_rag("任何查询", persist_dir=str(tmp_path / "chroma")) == []
    assert rag_store.search_rag_terms("任何查询", persist_dir=str(tmp_path / "chroma")) == []


def test_seed_knowledge_without_dir(tmp_path):
    # 目录不存在 → 返回 0，不抛异常
    result = rag_store.seed_rag_knowledge(str(tmp_path / "chroma"),
                                          tmp_path / "no_templates", tmp_path / "no_terms.txt")
    assert result["templates"] == 0 and result["terms"] == 0


def test_main_imports_without_db(tmp_path):
    """main 模块可导入且不抛错（配合 conftest 隔离）。"""
    import main  # noqa: F401
    assert main.app.title == "SmartBrief API"
