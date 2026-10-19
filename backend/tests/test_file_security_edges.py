"""文件安全的长度、压缩及Office容器边界。"""

import io
import struct
import zipfile
import pytest
from services.file_security import (
    validate_component,
    validate_document,
    validate_archive,
    MAX_DOCUMENT_BYTES,
)


def zipped(entries, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


@pytest.mark.parametrize(
    "name", ["", ".", "..", "x" * 256, "bad\x7f.txt", "LPT9.txt", "COM¹.txt"]
)
def test_invalid_components(name):
    with pytest.raises(ValueError):
        validate_component(name)


def test_document_exact_limit_category_and_office_containers():
    assert (
        validate_document(b"x" * MAX_DOCUMENT_BYTES, "accepted.txt", "upload") == ".txt"
    )
    with pytest.raises(ValueError):
        validate_document(b"fixture", "good.txt", "unknown")
    container = zipped([("document.xml", "fixture")])
    assert validate_document(container, "fixture.docx", "upload") == ".docx"
    assert validate_document(container, "fixture.xlsx", "upload") == ".xlsx"
    with pytest.raises(ValueError):
        validate_document(b"not a zip", "fixture.xlsx", "upload")


@pytest.mark.parametrize(
    "data",
    [b"", b"x" * (2 * 1024 * 1024 + 1), b"invalid zip"],
    ids=["empty", "oversize", "invalid-zip"],
)
def test_invalid_archives(data):
    with pytest.raises(ValueError):
        validate_archive(data)


def test_empty_and_excessive_archive_entries():
    for data in [zipped([]), zipped([(f"{i}.txt", "fixture") for i in range(257)])]:
        with pytest.raises(ValueError):
            validate_archive(data)


def test_compression_ratio_and_encryption_rejected():
    with pytest.raises(ValueError):
        validate_archive(zipped([("bomb.txt", b"x" * 1500000)], zipfile.ZIP_DEFLATED))
    raw = bytearray(zipped([("fixture.txt", "data")]))
    central = raw.index(b"PK\x01\x02")
    struct.pack_into("<H", raw, 6, 1)
    struct.pack_into("<H", raw, central + 8, 1)
    with pytest.raises(ValueError):
        validate_archive(bytes(raw))


@pytest.mark.parametrize("name", ["/", "a//b", "../x", "C:/x"])
def test_invalid_archive_components(name):
    with pytest.raises(ValueError):
        validate_archive(zipped([(name, "fixture")]))


def test_zip_embedded_null_name_rejected():
    raw = zipped([("safe.txt", "fixture")]).replace(b"safe.txt", b"safe\x00txt")
    with pytest.raises(ValueError):
        validate_archive(raw)
