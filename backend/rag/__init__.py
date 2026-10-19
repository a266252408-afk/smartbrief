"""RAG 知识库：多格式加载、递归分块、向量化与 ChromaDB 存储。

配置使用 backend/.env 的 EMBEDDING_*，数据保存在 backend/data/。
代码来源与适配说明见 docs/ATTRIBUTION.md。
"""
from rag.embeddings import Embeddings, SiliconFlowEmbeddings, create_embeddings
from rag.loaders import Document, DocumentLoader, load_document
from rag.chunkers import BaseChunker, RecursiveChunker
from rag.vectorstores import ChromaVectorStore

__all__ = [
    "Embeddings",
    "SiliconFlowEmbeddings",
    "create_embeddings",
    "Document",
    "DocumentLoader",
    "load_document",
    "BaseChunker",
    "RecursiveChunker",
    "ChromaVectorStore",
]
