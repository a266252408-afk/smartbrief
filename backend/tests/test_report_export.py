"""T11-02 报告导出与运行环境状态：来源、质量、生成版本和演示标识。"""
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from db import database, task_repository
from services.demo_data import environment_status
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client():
    database.init_db()
    from main import app
    return TestClient(app)


def _import(client, sample_ids):
    return client.post("/api/v2/demo/import", json={"sample_ids": sample_ids}, headers=ADMIN_HEADERS)


def test_demo_report_exports_sources_quality_and_generation_version(client):
    assert _import(client, ["normal-report"]).status_code == 200
    response = client.get("/api/v1/tasks/demo-normal-report/report", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    report = response.json()
    assert report["metadata"]["demo"] is True
    assert report["metadata"]["demo_sample_id"] == "normal-report"
    assert report["generator_version"] == "demo-1.0"
    assert report["quality_status"] == "passed" and report["forced_output"] is False
    assert [source["source_id"] for source in report["sources"]] == ["S1", "S2"]
    assert all(source["url"].startswith("https://example.invalid/") for source in report["sources"])
    assert report["citations"] and all(citation["valid"] for citation in report["citations"])
    assert "[S1]" in report["content_markdown"] and "[S2]" in report["content_markdown"]
    assert report["executive_summary"]["total_time"] is not None


def test_real_report_records_generation_version_and_old_report_stays_unknown(client):
    task_repository.create_task("task_real", "真实任务")
    task_repository.update_task("task_real", status="completed", quality_status="not_run", final_report="真实报告")
    report = client.get("/api/v1/tasks/task_real/report", headers=ADMIN_HEADERS).json()
    assert report["metadata"] == {"demo": False, "generator_version": "0.2.0",
                                  "demo_sample_id": None, "demo_sample_version": None}
    assert report["generator_version"] == "0.2.0"

    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute("UPDATE tasks SET metadata='{}' WHERE task_id='task_real'")
    old = client.get("/api/v1/tasks/task_real/report", headers=ADMIN_HEADERS).json()
    assert old["metadata"] == {} and old["generator_version"] is None

    assert _import(client, ["service-recovery"]).status_code == 200
    unavailable = client.get("/api/v1/tasks/demo-service-recovery/report", headers=ADMIN_HEADERS)
    assert unavailable.status_code == 404


def test_environment_reports_offline_without_keys_and_no_secret_echo(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    response = client.get("/api/v2/demo/environment", headers=READ_HEADERS)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["execution_mode"] == "offline"
    assert data["llm_configured"] is False and data["embedding_configured"] is False
    assert data["demo_available"] is True and data["demo_count"] == 3
    assert "未配置模型 Key" in data["message"]
    assert "可导入演示样例" in data["message"]


def test_environment_reports_online_without_echoing_keys(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fixture-secret-value")
    monkeypatch.setenv("EMBEDDING_API_KEY", "embed-fixture-secret")
    response = client.get("/api/v2/demo/environment", headers=READ_HEADERS)
    data = response.json()["data"]
    assert data["execution_mode"] == "online"
    assert data["llm_configured"] is True and data["embedding_configured"] is True
    assert "fixture-secret" not in response.text


def test_environment_marks_demo_unavailable_without_breaking_status(client, monkeypatch):
    from services.demo_data import DemoImportError
    monkeypatch.setattr("services.demo_data.load_samples", lambda: (_ for _ in ()).throw(DemoImportError("DEMO_DATA_MISSING", "x", 503)))
    response = client.get("/api/v2/demo/environment", headers=ADMIN_HEADERS)
    data = response.json()["data"]
    assert data["demo_available"] is False and data["demo_count"] == 0
    assert data["execution_mode"] in {"online", "offline"}

    monkeypatch.setattr("api.demo_routes.environment_status", lambda: (_ for _ in ()).throw(RuntimeError("SECRET")))
    failed = client.get("/api/v2/demo/environment", headers=ADMIN_HEADERS)
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "ENVIRONMENT_UNAVAILABLE"
    assert "SECRET" not in failed.text


def test_environment_contract_matches_runtime():
    from jsonschema import Draft202012Validator
    schema = json.loads((ROOT / "docs/schemas/contracts.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    data = environment_status()
    assert list(validator.iter_errors({"$ref": "#/$defs/DemoEnvironment", **data})) == []
