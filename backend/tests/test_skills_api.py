"""技能管理 API 测试：/api/v1/skills 全套路由。"""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def skills_client(tmp_path, monkeypatch):
    """隔离技能注册表与技能池目录后启动应用。"""
    from skills import registry as reg_mod
    from skills import skills_manager as sm_mod

    monkeypatch.setattr(reg_mod, "registry", reg_mod.SkillRegistry())
    monkeypatch.setattr(sm_mod, "SKILLS_POOL_DIR", tmp_path / "skills_pool")
    monkeypatch.setattr(sm_mod, "SKILL_CONFIG_PATH", tmp_path / "skill_config.json")
    monkeypatch.setattr("mcp_client.mcp_client.CONFIG_PATH", tmp_path / "mcp_config.json")
    monkeypatch.setattr("tools.builtin_tools.CONFIG_PATH", tmp_path / "tool_config.json")
    from main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- 技能列表


def test_list_skills(skills_client):
    resp = skills_client.get("/api/v1/skills")
    assert resp.status_code == 200
    skills = resp.json()["skills"]
    assert {s["name"] for s in skills} == {
        "builtin_search", "builtin_analyze", "builtin_write", "builtin_review"
    }
    assert all(s["builtin"] is True for s in skills)
    assert all(s["source"] == "builtin" for s in skills)


# ---------------------------------------------------------------- 新增技能


def test_create_skill_api(skills_client):
    resp = skills_client.post(
        "/api/v1/skills",
        json={"name": "my_skill", "display_name": "我的技能", "description": "测试", "category": "search"},
    )
    assert resp.status_code == 200
    skill = resp.json()["skill"]
    assert skill["name"] == "my_skill"
    assert skill["builtin"] is False
    assert len(skills_client.get("/api/v1/skills").json()["skills"]) == 5


def test_create_skill_api_invalid_name_422(skills_client):
    resp = skills_client.post(
        "/api/v1/skills", json={"name": "../bad", "display_name": "x", "category": "search"}
    )
    assert resp.status_code == 422


def test_create_skill_api_invalid_category_422(skills_client):
    resp = skills_client.post(
        "/api/v1/skills",
        json={"name": "s1", "display_name": "x", "category": "hack"},
    )
    assert resp.status_code == 422  # pydantic pattern 拦截


# ---------------------------------------------------------------- 编辑技能


def test_update_skill_api(skills_client):
    skills_client.post(
        "/api/v1/skills",
        json={"name": "my_skill", "display_name": "旧", "description": "d", "category": "search"},
    )
    resp = skills_client.put("/api/v1/skills/my_skill", json={"description": "新描述"})
    assert resp.status_code == 200
    assert resp.json()["skill"]["description"] == "新描述"


def test_update_skill_api_builtin_forbidden(skills_client):
    resp = skills_client.put("/api/v1/skills/builtin_search", json={"description": "x"})
    assert resp.status_code == 400
    assert "内置技能不可修改" in resp.json()["detail"]


def test_update_skill_api_not_found(skills_client):
    resp = skills_client.put("/api/v1/skills/nope", json={"description": "x"})
    assert resp.status_code == 404


# ---------------------------------------------------------------- 删除技能


def test_delete_skill_api(skills_client):
    skills_client.post(
        "/api/v1/skills", json={"name": "my_skill", "display_name": "旧", "category": "search"}
    )
    resp = skills_client.delete("/api/v1/skills/my_skill")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == "my_skill"


def test_delete_skill_api_builtin_forbidden(skills_client):
    resp = skills_client.delete("/api/v1/skills/builtin_search")
    assert resp.status_code == 400
    assert "内置技能不可删除" in resp.json()["detail"]


def test_delete_skill_api_not_found(skills_client):
    resp = skills_client.delete("/api/v1/skills/nope")
    assert resp.status_code == 404


# ---------------------------------------------------------------- 技能开关


def test_toggle_skill_api_custom(skills_client):
    skills_client.post(
        "/api/v1/skills",
        json={"name": "my_skill", "display_name": "我的技能", "category": "search"},
    )
    resp = skills_client.put("/api/v1/skills/my_skill/enabled", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["skill"]["enabled"] is False
    # 列表同步反映开关状态
    skills = skills_client.get("/api/v1/skills").json()["skills"]
    target = next(s for s in skills if s["name"] == "my_skill")
    assert target["enabled"] is False


def test_toggle_skill_api_builtin(skills_client):
    resp = skills_client.put("/api/v1/skills/builtin_search/enabled", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["skill"]["enabled"] is False


def test_toggle_skill_api_not_found(skills_client):
    resp = skills_client.put("/api/v1/skills/nope/enabled", json={"enabled": False})
    assert resp.status_code == 404


def test_run_disabled_skill_api_400(skills_client):
    skills_client.post(
        "/api/v1/skills",
        json={"name": "my_skill", "display_name": "我的技能", "category": "search"},
    )
    skills_client.put("/api/v1/skills/my_skill/enabled", json={"enabled": False})
    resp = skills_client.post("/api/v1/skills/my_skill/run", json={"query": "hi"})
    assert resp.status_code == 400
    assert "已禁用" in resp.json()["detail"]


# ---------------------------------------------------------------- zip 导入


def test_import_skill_zip_api(skills_client):
    md = "---\nname: zip_skill\ndisplay_name: ZIP技能\ncategory: review\n---\n\n正文"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("skill.md", md)
    resp = skills_client.post(
        "/api/v1/skills/import",
        files={"file": ("s.zip", buf.getvalue(), "application/zip")},
    )
    assert resp.status_code == 200
    assert resp.json()["skill"]["name"] == "zip_skill"


def test_import_skill_zip_api_invalid(skills_client):
    resp = skills_client.post(
        "/api/v1/skills/import",
        files={"file": ("s.zip", b"not a zip", "application/zip")},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------- 试运行


def test_run_skill_api(skills_client, monkeypatch):
    from types import SimpleNamespace

    async def fake_call_tool(name, arguments=None):
        return {"source": "test", "query": "储能", "results": [{"title": "t"}]}

    monkeypatch.setattr(
        "skills.builtin_skills.mcp_manager", SimpleNamespace(call_tool=fake_call_tool)
    )
    resp = skills_client.post("/api/v1/skills/builtin_search/run", json={"query": "储能"})
    assert resp.status_code == 200
    out = resp.json()["output"]
    assert out["status"] == "success"
    assert out["data"]["query"] == "储能"
    assert out["metadata"]["mode"] == "web"


def test_run_skill_api_not_found(skills_client):
    resp = skills_client.post("/api/v1/skills/nope/run", json={"query": "hi"})
    assert resp.status_code == 404
