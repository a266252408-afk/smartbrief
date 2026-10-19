"""上传名、体积、服务端路径与ZIP拒绝边界。"""

import io
import stat
import zipfile
import pytest
from fastapi.testclient import TestClient
from tests.auth_support import ADMIN_HEADERS
from rag import store


def archive(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in entries:
            z.writestr(name, content)
    return out.getvalue()


@pytest.mark.parametrize(
    "name",
    [
        "../escape.txt",
        "..\\escape.txt",
        "/absolute.txt",
        "C:drive.txt",
        "C:\\absolute.txt",
        "a/b.txt",
        "a\\b.txt",
        "CON.txt",
        "bad.txt.",
        "bad.txt ",
        "file.exe",
        "a\x00.txt",
    ],
)
def test_invalid_name_never_writes(name, tmp_path):
    with pytest.raises(ValueError):
        store.ingest_file(b"fixture", name)
    assert not store.UPLOADS_DIR.exists()
    assert not (tmp_path / "escape.txt").exists()


def test_same_display_name_uses_distinct_storage(fake_embeddings):
    store._embeddings = fake_embeddings
    first = store.ingest_file(b"first fixture", "same.txt")
    second = store.ingest_file(b"second fixture", "same.txt")
    assert first["source"] == second["source"] == "same.txt"
    assert first["display_name"] == "same.txt"
    assert first["document_id"] != second["document_id"]
    files = list(store.UPLOADS_DIR.iterdir())
    assert len(files) == 2 and all(p.name != "same.txt" for p in files)
    assert {p.read_bytes() for p in files} == {b"first fixture", b"second fixture"}


def test_empty_and_large_documents_leave_no_file():
    for data in [b"", b"x" * (10 * 1024 * 1024 + 1)]:
        with pytest.raises(ValueError):
            store.ingest_file(data, "fixture.txt")
    assert not store.UPLOADS_DIR.exists()


def test_parse_failure_removes_new_file():
    with pytest.raises(Exception):
        store.ingest_file(b"not a pdf", "broken.pdf")
    assert not list(store.UPLOADS_DIR.glob("*"))


@pytest.mark.parametrize(
    "entry",
    [
        "../evil.txt",
        "C:evil.txt",
        "folder/../../evil.txt",
        "/evil.txt",
        "folder\\..\\evil.txt",
        "folder/CON.txt",
        "folder/a:stream",
    ],
)
def test_zip_paths_are_rejected_before_extraction(entry, tmp_path):
    from skills.skills_manager import SkillsManager

    pool = tmp_path / "pool"
    with pytest.raises(ValueError):
        SkillsManager(pool).import_skill(
            archive(
                [
                    ("skill.md", "---\nname: security-fixture\n---\nfixture"),
                    (entry, "fixture"),
                ]
            )
        )
    assert not pool.exists()


def test_zip_symlink_and_duplicate_paths_are_rejected(tmp_path):
    from skills.skills_manager import SkillsManager

    for symlink in [True, False]:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("skill.md", "---\nname: security-fixture\n---\nfixture")
            if symlink:
                info = zipfile.ZipInfo("link")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(info, "../outside")
            else:
                z.writestr("A.txt", "one")
                z.writestr("a.txt", "two")
        with pytest.raises(ValueError):
            SkillsManager(tmp_path / "pool").import_skill(out.getvalue())
    assert not (tmp_path / "pool").exists()


def test_zip_bomb_is_rejected(tmp_path):
    from skills.skills_manager import SkillsManager

    data = archive(
        [
            ("skill.md", "---\nname: security-fixture\n---\nfixture"),
            ("bomb.txt", b"x" * (2 * 1024 * 1024 + 1)),
        ]
    )
    with pytest.raises(ValueError):
        SkillsManager(tmp_path / "pool").import_skill(data)
    assert not (tmp_path / "pool").exists()


def test_http_rejects_bad_upload_without_index(fake_embeddings):
    from main import app

    store._embeddings = fake_embeddings
    with TestClient(app, headers=ADMIN_HEADERS) as client:
        response = client.post(
            "/api/v1/knowledge/documents", files={"file": ("../bad.txt", b"fixture")}
        )
        assert response.status_code == 400
        assert client.get("/api/v1/knowledge/documents").json()["documents"] == []


def test_same_content_remains_bound_to_both_document_ids(fake_embeddings):
    store._embeddings = fake_embeddings
    one = store.ingest_file(b"identical fixture", "one.txt")
    two = store.ingest_file(b"identical fixture", "two.txt")
    rows = store.get_store().get_documents()
    assert {row["metadata"]["document_id"] for row in rows} == {
        one["document_id"],
        two["document_id"],
    }


def test_storage_collision_does_not_remove_original(fake_embeddings, monkeypatch):
    from types import SimpleNamespace

    store._embeddings = fake_embeddings
    monkeypatch.setattr(store, "uuid4", lambda: SimpleNamespace(hex="f" * 32))
    store.ingest_file(b"original fixture", "same.txt")
    with pytest.raises(FileExistsError):
        store.ingest_file(b"new fixture", "same.txt")
    assert (store.UPLOADS_DIR / ("f" * 32 + ".txt")).read_bytes() == b"original fixture"


def test_import_persistence_failure_cleans_only_new_directory(tmp_path, monkeypatch):
    from skills import skills_manager as module
    from skills import registry

    def fail():
        raise OSError("固定配置写入故障")

    monkeypatch.setattr(module, "_save_skill_config", fail)
    data = archive([("skill.md", "---\nname: cleanup-fixture\n---\nfixture")])
    with pytest.raises(OSError):
        module.SkillsManager(tmp_path / "pool").import_skill(data)
    assert not (tmp_path / "pool" / "cleanup-fixture").exists()
    assert registry.registry.get("cleanup-fixture") is None
