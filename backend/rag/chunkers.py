"""文本分块：按分隔符递归切分并保留重叠上下文。来源说明见 docs/ATTRIBUTION.md。"""
from typing import List

from rag.loaders import Document


class BaseChunker:
    """分块器抽象基类：chunk() 接收 Document 列表，返回切分后的 Document 列表。"""

    def chunk(self, documents: List[Document]) -> List[Document]:
        raise NotImplementedError


class RecursiveChunker(BaseChunker):
    """递归分块器：按分隔符优先级逐级切分，尽量保持块内语义完整。

    分隔符优先级：段落 "\n\n" > 换行 "\n" > 句号 "。" "." > 逗号 "，" "," > 空格 > 字符兜底
    """

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 64):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = ["\n\n", "\n", "。", ".", "，", ",", " ", ""]

    def chunk(self, documents: List[Document]) -> List[Document]:
        chunks = []
        for doc in documents:
            text = doc.page_content
            for i, chunk_text in enumerate(self._recursive_split(text)):
                chunks.append(Document(
                    page_content=chunk_text,
                    metadata={
                        **doc.metadata,
                        "chunk_index": i,
                        "chunk_size": len(chunk_text),
                    },
                ))
        return chunks

    def _recursive_split(self, text: str) -> List[str]:
        """递归切分文本。

        流程：文本不超长直接返回 → 找到第一个出现的分隔符切分（贪心合并小块）
        → 对仍然过大的块递归（用更低优先级分隔符）→ 全失败时按字符兜底。
        """
        if len(text) <= self.chunk_size:
            return [text]

        chunks = []
        for sep in self.separators:
            if sep == "":
                # 兜底：按字符强制切分（带重叠）
                chunks = self._split_by_char(text)
                break
            if sep in text:
                segments = text.split(sep)
                current_chunk = ""
                for segment in segments:
                    candidate = current_chunk + sep + segment if current_chunk else segment
                    if len(candidate) <= self.chunk_size:
                        current_chunk = candidate
                    else:
                        if current_chunk:
                            chunks.append(current_chunk)
                        current_chunk = segment
                if current_chunk:
                    chunks.append(current_chunk)
                break  # 命中有效分隔符后跳出循环

        # 对仍然过大的块递归处理（用更低优先级分隔符）
        result = []
        for chunk in chunks:
            if len(chunk) > self.chunk_size:
                result.extend(self._recursive_split(chunk))
            else:
                result.append(chunk)
        return result

    def _split_by_char(self, text: str) -> List[str]:
        """按字符强制切分（最细粒度兜底，支持重叠）。"""
        return [
            text[i:i + self.chunk_size]
            for i in range(0, len(text), self.chunk_size - self.chunk_overlap)
        ]
