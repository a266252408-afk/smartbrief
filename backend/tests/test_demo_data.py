"""T11-01 显式演示样例：导入、演示标识、权限与禁止真实生成。"""
import copy
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from db import database, task_repository, usage_repository
from services import demo_data
from services.demo_data import DemoImportError, DemoSample
from services.task_metadata import TaskMetadata
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS

ROOT = Path(__file__).resolve().parents[2]
NORMAL = json.loads((ROOT / "backend/data/demo/normal-report.json").read_text(encoding="utf-8"))


@pytest.fixture
def client():
    database.init_db()
    from main import app
    return TestClient(app)


def _import(client, body=None):
    return client.post("/api/v2/demo/import", json={} if body is None else body, headers=ADMIN_HEADERS)


def test_samples_are_explicit_and_never_queued():
    samples = demo_data.load_samples()
    assert {sample.sample_id for sample in samples} == {"normal-report", "document-trace", "service-recovery"}
    assert {sample.scenario for sample in samples} == {"normal", "document", "recovery"}
    for sample in samples:
        assert sample.task_id.startswith("demo-")
        assert "演示" in sample.title
        assert sample.task.status in {"completed", "paused"}
        assert sample.task.execution_status == sample.task.status
        assert sample.task.status != "running"
        assert all(entry.usage_known is False for entry in sample.usage)


def test_import_persists_demo_markers_without_generation(client):
    response = _import(client)
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert len(body["imported"]) == 3 and body["skipped"] == []

    normal = task_repository.get_task("demo-normal-report")
    assert normal["metadata"] == {
        "demo": True, "generator_version": "demo-1.0",
        "demo_sample_id": "normal-report", "demo_sample_version": "1.0",
    }
    assert normal["status"] == normal["execution_status"] == "completed"
    assert normal["quality_status"] == "passed"
    assert len(normal["evidence"]) == 2
    assert normal["citations"] and all(item["valid"] for item in normal["citations"])
    assert "[S1]" in normal["final_report"]
    assert task_repository.get_logs("demo-normal-report")
    usage = usage_repository.list_usage("demo-normal-report")
    assert len(usage) == 1 and usage[0]["usage_known"] is False
    assert usage[0]["input_tokens"] is None and usage[0]["estimated_cost"] is None

    document = task_repository.get_task("demo-document-trace")
    assert document["retrievals"][0]["status"] == "matched"
    assert document["evidence"][0]["chunk_id"] == "demo-chunk-137"
    assert document["evidence"][0]["page"] == 2

    recovery = task_repository.get_task("demo-service-recovery")
    assert recovery["status"] == "paused" and recovery["final_report"] is None
    assert recovery["error_code"] == "SERVICE_INTERRUPTED"

    items, total = task_repository.list_tasks()
    assert total == 3
    assert not [item for item in items if item["execution_status"] in {"queued", "running"}]
    assert all(item["metadata"]["demo"] is True for item in items)

    detail = client.get("/api/v2/tasks/demo-normal-report", headers=ADMIN_HEADERS).json()["data"]
    assert detail["metadata"]["demo"] is True and detail["metadata"]["generator_version"] == "demo-1.0"
    assert detail["execution_status"] == "completed" and detail["quality_status"] == "passed"

    report = client.get("/api/v1/tasks/demo-normal-report/report", headers=ADMIN_HEADERS).json()
    assert report["metadata"]["demo"] is True
    assert report["generator_version"] == "demo-1.0"
    assert report["quality_status"] == "passed"
    assert report["sources"]


def test_import_is_idempotent_and_never_overwrites_user_data(client):
    task_repository.create_task("demo-normal-report", "用户真实任务")
    task_repository.update_task("demo-normal-report", status="completed", final_report="用户报告")

    conflict = _import(client)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "DEMO_CONFLICT"
    assert task_repository.get_task("demo-normal-report")["final_report"] == "用户报告"
    assert task_repository.list_tasks()[1] == 1

    task_repository.delete_task("demo-normal-report")
    assert _import(client).status_code == 200
    logs = len(task_repository.get_logs("demo-normal-report"))
    usage = len(usage_repository.list_usage("demo-normal-report"))

    again = _import(client)
    assert again.status_code == 200
    assert again.json()["data"]["imported"] == []
    assert len(again.json()["data"]["skipped"]) == 3
    assert len(task_repository.get_logs("demo-normal-report")) == logs
    assert len(usage_repository.list_usage("demo-normal-report")) == usage


def test_import_requires_admin_and_rejects_bad_selection(client):
    assert client.post("/api/v2/demo/import", json={}).status_code == 401
    assert client.post("/api/v2/demo/import", json={}, headers=READ_HEADERS).status_code == 403
    samples = client.get("/api/v2/demo/samples", headers=READ_HEADERS)
    assert samples.status_code == 200
    assert {item["sample_id"] for item in samples.json()["data"]["items"]} == {
        "normal-report", "document-trace", "service-recovery"
    }
    assert all(item["imported"] is False for item in samples.json()["data"]["items"])

    unknown = _import(client, {"sample_ids": ["missing-sample"]})
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "DEMO_SAMPLE_NOT_FOUND"
    duplicate = _import(client, {"sample_ids": ["normal-report", "normal-report"]})
    assert duplicate.status_code == 422 and duplicate.json()["error"]["code"] == "DEMO_SAMPLE_DUPLICATED"
    invalid = _import(client, {"sample_ids": ["normal-report"], "extra": True})
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "INVALID_DEMO_REQUEST"
    assert task_repository.list_tasks()[1] == 0


def test_demo_tasks_reject_cancel_resume_and_regenerate(client):
    assert _import(client, {"sample_ids": ["service-recovery"]}).status_code == 200
    before = task_repository.get_task("demo-service-recovery")
    log_count = len(task_repository.get_logs("demo-service-recovery"))

    resume = client.post("/api/v2/tasks/demo-service-recovery/resume", headers=ADMIN_HEADERS)
    assert resume.status_code == 409 and resume.json()["error"]["code"] == "DEMO_NOT_EXECUTABLE"
    cancel = client.post("/api/v2/tasks/demo-service-recovery/cancel", headers=ADMIN_HEADERS)
    assert cancel.status_code == 409 and cancel.json()["error"]["code"] == "DEMO_NOT_EXECUTABLE"
    regenerate = client.post("/api/v2/tasks/demo-service-recovery/regenerate", headers=ADMIN_HEADERS)
    assert regenerate.status_code == 409 and regenerate.json()["error"]["code"] == "DEMO_NOT_EXECUTABLE"

    after = task_repository.get_task("demo-service-recovery")
    assert after["status"] == before["status"] == "paused"
    assert after["updated_at"] == before["updated_at"]
    assert len(task_repository.get_logs("demo-service-recovery")) == log_count
    assert task_repository.list_tasks()[1] == 1


def test_legacy_task_without_metadata_migrates_to_empty_object(tmp_path):
    path = tmp_path / "v2.db"
    database.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE tasks DROP COLUMN metadata")
        conn.execute("DELETE FROM schema_migrations WHERE version=3")
        conn.execute("PRAGMA user_version=2")
        conn.execute("INSERT INTO tasks(task_id,topic,status,execution_status,quality_status) VALUES('old','旧任务','completed','completed','unknown')")
    database.init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == database.SCHEMA_VERSION
        assert [row[0] for row in conn.execute("SELECT version FROM schema_migrations")] == [1, 2, 3, 4]
        assert conn.execute("SELECT metadata FROM tasks WHERE task_id='old'").fetchone()[0] == "{}"
    assert task_repository.get_task("old")["metadata"] == {}


def test_contract_schema_matches_demo_payloads(client):
    from jsonschema import Draft202012Validator
    schema = json.loads((ROOT / "docs/schemas/contracts.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    for item in demo_data.list_samples():
        assert list(validator.iter_errors({"$ref": "#/$defs/DemoSample", **item})) == []
    result = _import(client).json()["data"]
    assert list(validator.iter_errors({"$ref": "#/$defs/DemoImportResult", **result})) == []
    meta = TaskMetadata(demo=True, generator_version="demo-1.0",
                        demo_sample_id="normal-report", demo_sample_version="1.0").model_dump()
    assert list(validator.iter_errors({"$ref": "#/$defs/TaskMetadata", **meta})) == []
    with pytest.raises(ValueError):
        TaskMetadata(demo=True, generator_version="demo-1.0")


def _mutated(change):
    sample = copy.deepcopy(NORMAL)
    change(sample)
    return sample


@pytest.mark.parametrize("change", [
    lambda s: s["task"].update(execution_status="paused"),
    lambda s: s["task"].update(final_report=""),
    lambda s: s["task"].update(status="paused", execution_status="paused"),
    lambda s: s["task"].update(completed_steps=[1, 1]),
    lambda s: s["task"].update(completed_steps=[1, 9]),
    lambda s: s["task"]["plan"][0].update(depends_on=[2]),
    lambda s: s["task"].update(current_step_index=1),
    lambda s: s["task"].update(retrievals=[{
        "task_id": "demo-other", "document_ids": [], "query": "演示",
        "options": {"top_k": 5, "threshold": 0.35, "context_budget": 6000},
        "status": "not_selected", "evidence": [], "error_code": None}]),
    lambda s: s["usage"][0].update(task_id="demo-other"),
    lambda s: s["usage"].append(copy.deepcopy(s["usage"][0])),
    lambda s: s["task"].update(evidence=[], final_report="137 台设备。"),
])
def test_invalid_demo_payload_is_rejected(change):
    with pytest.raises(ValueError):
        DemoSample.model_validate(_mutated(change))


def test_invalid_sample_directory_and_duplicates_are_rejected(tmp_path):
    with pytest.raises(DemoImportError) as missing:
        demo_data.load_samples(tmp_path)
    assert missing.value.code == "DEMO_DATA_MISSING" and missing.value.status == 503

    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(DemoImportError) as broken:
        demo_data.load_samples(tmp_path)
    assert broken.value.code == "DEMO_DATA_INVALID"

    tmp_path.joinpath("broken.json").unlink()
    text = NORMAL
    (tmp_path / "a.json").write_text(json.dumps(text, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(text, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(DemoImportError) as duplicate:
        demo_data.load_samples(tmp_path)
    assert duplicate.value.code == "DEMO_DATA_INVALID"


def test_task_metadata_rejects_blank_and_non_demo_sample_fields():
    with pytest.raises(ValueError):
        TaskMetadata(generator_version=" ")
    with pytest.raises(ValueError):
        TaskMetadata(demo=False, demo_sample_id="normal-report")


def test_corrupt_metadata_is_not_treated_as_demo(client):
    task_repository.create_task("demo-normal-report", "用户真实任务")
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute("UPDATE tasks SET metadata='not-json' WHERE task_id='demo-normal-report'")
    response = _import(client, {"sample_ids": ["normal-report"]})
    assert response.status_code == 409 and response.json()["error"]["code"] == "DEMO_CONFLICT"
    assert task_repository.get_task("demo-normal-report")["topic"] == "用户真实任务"


def test_demo_routes_map_data_and_import_failures(client, monkeypatch):
    def missing():
        raise DemoImportError("DEMO_DATA_MISSING", "演示样例文件不可用", 503)

    monkeypatch.setattr("api.demo_routes.list_samples", missing)
    response = client.get("/api/v2/demo/samples", headers=ADMIN_HEADERS)
    assert response.status_code == 503 and response.json()["error"]["code"] == "DEMO_DATA_MISSING"

    monkeypatch.setattr("api.demo_routes.list_samples", lambda: (_ for _ in ()).throw(RuntimeError("SECRET")))
    response = client.get("/api/v2/demo/samples", headers=ADMIN_HEADERS)
    assert response.status_code == 503 and response.json()["error"]["code"] == "DEMO_DATA_UNAVAILABLE"
    assert "SECRET" not in response.text

    monkeypatch.setattr("api.demo_routes.import_samples", lambda ids: (_ for _ in ()).throw(RuntimeError("SECRET")))
    response = _import(client)
    assert response.status_code == 503 and response.json()["error"]["code"] == "DEMO_IMPORT_FAILED"
    assert "SECRET" not in response.text
    assert task_repository.list_tasks()[1] == 0
