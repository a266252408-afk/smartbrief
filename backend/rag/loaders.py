"""多格式文档加载器：统一解析为 Document。来源说明见 docs/ATTRIBUTION.md。"""
import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class Document:
    """统一文档数据模型：page_content（文本）+ metadata（来源/类型等元信息）。"""

    page_content: str
    metadata: dict = field(default_factory=dict)


class BaseLoader:
    """加载器抽象基类：子类必须实现 load()。"""

    def load(self, file_path: str) -> List[Document]:
        raise NotImplementedError


class TextLoader(BaseLoader):
    """.txt 纯文本加载器（UTF-8）。"""

    def load(self, file_path: str) -> List[Document]:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        return [Document(
            page_content=text,
            metadata={"source": file_path, "file_type": "txt"},
        )]


class MarkdownLoader(BaseLoader):
    """.md Markdown 加载器（本质为纯文本，标记 file_type=markdown）。"""

    def load(self, file_path: str) -> List[Document]:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        return [Document(
            page_content=text,
            metadata={"source": file_path, "file_type": "markdown"},
        )]


class DocxLoader(BaseLoader):
    """.docx Word 加载器（python-docx，仅提取文本段落）。"""

    def load(self, file_path: str) -> List[Document]:
        try:
            from docx import Document as DocxDocument
        except ImportError as exc:  # pragma: no cover - 依赖缺失场景
            raise ImportError("python-docx 未安装，无法加载 .docx 文件") from exc
        doc = DocxDocument(file_path)
        paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
        return [Document(
            page_content="\n".join(paragraphs),
            metadata={"source": file_path, "file_type": "docx"},
        )]


class PDFLoader(BaseLoader):
    """.pdf 加载器（PyPDF2）：每页生成一个独立 Document（保留页码便于溯源）。"""

    def load(self, file_path: str) -> List[Document]:
        try:
            import PyPDF2
        except ImportError as exc:  # pragma: no cover - 依赖缺失场景
            raise ImportError("PyPDF2 未安装，无法加载 .pdf 文件") from exc
        docs = []
        with open(file_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for i, page in enumerate(reader.pages):
                text = page.extract_text()
                if text and text.strip():
                    docs.append(Document(
                        page_content=text,
                        metadata={
                            "source": file_path,
                            "file_type": "pdf",
                            "page_number": i + 1,
                        },
                    ))
        return docs


class ExcelLoader(BaseLoader):
    """.xlsx / .xls 加载器（openpyxl）：每个 sheet 生成一个 Document。"""

    def load(self, file_path: str) -> List[Document]:
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover - 依赖缺失场景
            raise ImportError("openpyxl 未安装，无法加载 .xlsx 文件") from exc
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        docs = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows_text = []
            for row in ws.iter_rows(values_only=True):
                row_values = [str(cell) for cell in row if cell is not None]
                if row_values:
                    rows_text.append(" | ".join(row_values))
            if rows_text:
                docs.append(Document(
                    page_content="\n".join(rows_text),
                    metadata={
                        "source": file_path,
                        "file_type": "excel",
                        "sheet_name": sheet_name,
                    },
                ))
        return docs


class DocumentLoader:
    """统一加载入口（策略上下文）：按扩展名分发到对应 Loader。"""

    loaders = {
        ".txt": TextLoader(),
        ".md": MarkdownLoader(),
        ".docx": DocxLoader(),
        ".pdf": PDFLoader(),
        ".xlsx": ExcelLoader(),
        ".xls": ExcelLoader(),
    }

    def load(self, file_path: str) -> List[Document]:
        ext = os.path.splitext(file_path)[1].lower()
        loader = self.loaders.get(ext)
        if loader is None:
            raise ValueError(f"不支持的文件类型：{ext}")
        return loader.load(file_path)


def load_document(file_path: str) -> List[Document]:
    """便捷函数：加载单个文件为 Document 列表（供知识库上传等场景直接调用）。"""
    return DocumentLoader().load(file_path)
