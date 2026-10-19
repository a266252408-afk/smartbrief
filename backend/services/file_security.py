"""跨平台文件名及有界ZIP校验，拒绝在任何写入前完成。"""

import io
from pathlib import Path, PureWindowsPath
import stat
import unicodedata
import zipfile

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_ZIP_ENTRIES = 256
EXTENSIONS = {".txt", ".md", ".docx", ".pdf", ".xlsx"}
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_component(name):
    if not name or len(name) > 255 or name in {".", ".."} or name[-1] in {".", " "}:
        raise ValueError("非法路径或文件名")
    if any(c in name for c in '/\\:<>"|?*') or any(
        ord(c) < 32 or ord(c) == 127 for c in name
    ):
        raise ValueError("非法路径或文件名")
    if (
        PureWindowsPath(name).drive
        or unicodedata.normalize("NFKC", name).split(".")[0].upper() in _RESERVED
    ):
        raise ValueError("非法路径或保留文件名")
    return name


def validate_document(data, filename, category):
    validate_component(filename)
    ext = Path(filename).suffix.lower()
    if ext not in EXTENSIONS or category not in {"upload", "template", "terminology"}:
        raise ValueError("不支持的文件类型或分类")
    if not data or len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("文档为空或超过10MiB上限")
    if ext in {".docx", ".xlsx"}:
        validate_archive(data, limit=MAX_DOCUMENT_BYTES)
    return ext


def validate_archive(data, limit=MAX_ARCHIVE_BYTES):
    if not data or len(data) > limit:
        raise ValueError("技能包过大或为空")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ZIP_ENTRIES:
                raise ValueError("ZIP条目数量超限或为空")
            total = 0
            seen = set()
            for info in infos:
                name = info.orig_filename
                normalized = name.rstrip("/")
                if not normalized or name.startswith("/") or "\\" in name:
                    raise ValueError("ZIP包含非法路径")
                for part in normalized.split("/"):
                    validate_component(part)
                key = normalized.casefold()
                if (
                    key in seen
                    or stat.S_ISLNK(info.external_attr >> 16)
                    or info.flag_bits & 1
                ):
                    raise ValueError("ZIP包含重复路径、链接或加密条目")
                seen.add(key)
                total += info.file_size
                if total > limit or info.file_size > max(info.compress_size, 1) * 1000:
                    raise ValueError("技能包解压后体积过大或压缩比超限")
            return infos
    except (zipfile.BadZipFile, UnicodeError) as exc:
        raise ValueError("不是有效的zip文件") from exc
