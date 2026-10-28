"""技能模块测试（skills/）：抽象层、注册表、内置技能、技能管理器。"""
import asyncio
import io
import zipfile
from types import SimpleNamespace

import pytest

from llm import client as llm_client
from skills import registry as reg_mod
from skills.base_skill import SkillInput, SkillOutput
from skills.builtin_skills import BUILTIN_SKILLS
from skills.skills_manager import MarkdownSkill, SkillsManager, _parse_skill_md


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """隔离注册表、技能池目录与内置技能配置路径，并注册内置技能。"""
    monkeypatch.setattr(reg_mod, "registry", reg_mod.SkillRegistry())
    monkeypatch.setattr(
        "skills.skills_manager.SKILL_CONFIG_PATH", tmp_path / "skill_config.json"
    )
    mgr = SkillsManager(pool_dir=tmp_path / "skills_pool")
    mgr.init()
    return mgr


def _make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


# ---------------------------------------------------------------- base_skill


def test_skill_input_validation():
    with pytest.raises(ValueError):
        SkillInput(query="")


def test_skill_output_defaults():
    out = SkillOutput(status="success")
    assert out.data is None
    assert out.metadata == {}


def test_skill_output_status_validation():
    with pytest.raises(ValueError):
        SkillOutput(status="pending")


# ---------------------------------------------------------------- registry


def test_registry_register_duplicate(manager):
    skill = reg_mod.registry.get("builtin_search")
    with pytest.raises(ValueError, match="已存在"):
        reg_mod.registry.register(skill)


def test_registry_unregister_builtin_forbidden(manager):
    with pytest.raises(ValueError, match="内置技能不可删除"):
        reg_mod.registry.unregister("builtin_search")


def test_registry_unregister_unknown(manager):
    with pytest.raises(KeyError):
        reg_mod.registry.unregister("nope")


def test_registry_get_all_and_source(manager):
    names = {s.name for s in reg_mod.registry.get_all()}
    assert names == {
        "builtin_search", "builtin_exa_search", "builtin_analyze", "builtin_write", "builtin_review"
    }
    assert reg_mod.registry.get_source("builtin_search") == "builtin"
    assert reg_mod.registry.get("nope") is None
    assert reg_mod.registry.get_source("nope") == ""


def test_registry_clear(manager):
    reg_mod.registry.clear()
    assert reg_mod.registry.get_all() == []


# ---------------------------------------------------------------- 内置技能


def test_builtin_skills_categories_and_execute(manager, monkeypatch):
    expected = {
        "builtin_search": "search",
        "builtin_exa_search": "search",
        "builtin_analyze": "analysis",
        "builtin_write": "writing",
        "builtin_review": "review",
    }

    # 隔离外部依赖：MCP / EXA 搜索走假实现，其余走 LLM 假调用
    async def fake_call_tool(name, arguments=None):
        return {"source": "test", "query": arguments.get("query", ""), "results": []}

    def fake_exa_search(query, num_results=5):
        return {"source": "exa", "query": query, "results": []}

    async def fake_chat(messages, tools=None, tool_choice=None):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{\"conclusion\": \"结论\", \"points\": [\"要点\"]}"))]
        )

    monkeypatch.setattr("skills.builtin_skills.mcp_manager", SimpleNamespace(call_tool=fake_call_tool))
    monkeypatch.setattr("skills.builtin_skills.exa_web_search", fake_exa_search)
    monkeypatch.setattr("skills.builtin_skills.llm_client", SimpleNamespace(chat=fake_chat))

    for cls in BUILTIN_SKILLS:
        skill = cls()
        assert skill.builtin is True
        assert skill.category == expected[skill.name]
        assert skill.to_dict()["source"] == "builtin"
        out = asyncio.run(skill.execute(SkillInput(query="测试")))
        assert out.status == "success"
        assert out.data["query"] == "测试"


@pytest.mark.parametrize("payload", [None, {"error": "搜索不可用", "results": []}])
def test_search_skill_reports_invalid_or_failed_tool_result(manager, monkeypatch, payload):
    async def fake_call_tool(*args, **kwargs):
        return payload

    monkeypatch.setattr("skills.builtin_skills.mcp_manager", SimpleNamespace(call_tool=fake_call_tool))
    out = asyncio.run(manager.run_skill("builtin_search", "测试"))
    assert out.status == "failed"
    assert out.data is None
    assert out.metadata["error"]


def test_exa_search_skill_reports_error(manager, monkeypatch):
    """EXA 未配置 Key / 接口失败时，技能返回 failed 而不是空结果成功。"""
    def fake_exa_search(query, num_results=5):
        return {"source": "exa", "query": query, "results": [], "error": "未配置 EXA_API_KEY"}

    monkeypatch.setattr("skills.builtin_skills.exa_web_search", fake_exa_search)
    out = asyncio.run(manager.run_skill("builtin_exa_search", "测试"))
    assert out.status == "failed"
    assert out.data is None
    assert "EXA_API_KEY" in out.metadata["error"]


@pytest.mark.parametrize("action", ["search", "analyze", "write", "review"])
def test_legacy_skill_switch_migrates_without_changing_state(manager, action):
    import json
    from skills import skills_manager

    config_path = skills_manager.SKILL_CONFIG_PATH
    config_path.write_text(json.dumps({"skills": {"mock_" + action: False}}), encoding="utf-8")
    manager.init()
    assert manager.get_skill("builtin_" + action).enabled is False
    persisted = json.loads(config_path.read_text(encoding="utf-8"))["skills"]
    assert persisted["builtin_" + action] is False
    assert "mock_" + action not in persisted


def test_new_skill_switch_overrides_legacy_value(manager):
    import json
    from skills import skills_manager

    skills_manager.SKILL_CONFIG_PATH.write_text(
        json.dumps({"skills": {"mock_" + "search": False, "builtin_search": True}}), encoding="utf-8"
    )
    manager.init()
    assert manager.get_skill("builtin_search").enabled is True


# ---------------------------------------------------------------- 管理器：初始化


def test_init_registers_builtin_only(manager):
    manager.init()
    assert len(reg_mod.registry.get_all()) == len(BUILTIN_SKILLS)


def test_init_loads_custom_skills_from_pool(manager):
    manager.create_skill(name="s1", display_name="技能一", category="search")
    manager.init()  # 模拟重启：清空后重新加载
    assert reg_mod.registry.get("s1") is not None
    assert reg_mod.registry.get("s1").builtin is False
    assert len(reg_mod.registry.get_all()) == len(BUILTIN_SKILLS) + 1


def test_init_skips_broken_dir(manager):
    broken = manager._pool_dir() / "broken"
    broken.mkdir(parents=True)
    (broken / "readme.txt").write_text("没有 skill.md", encoding="utf-8")
    manager.init()
    assert reg_mod.registry.get("broken") is None
    assert len(reg_mod.registry.get_all()) == len(BUILTIN_SKILLS)


# ---------------------------------------------------------------- 管理器：创建 / 更新 / 删除


def test_create_skill(manager):
    info = manager.create_skill(name="my_skill", display_name="我的技能", description="测试", category="search")
    assert info["name"] == "my_skill"
    assert info["display_name"] == "我的技能"
    assert info["builtin"] is False
    assert info["source"] == "custom"
    assert "## 技能说明" in info["skill_md"]
    md = (manager._pool_dir() / "my_skill" / "skill.md").read_text(encoding="utf-8")
    assert "name: my_skill" in md
    assert "category: search" in md


def test_create_skill_invalid_name(manager):
    with pytest.raises(ValueError, match="技能名"):
        manager.create_skill(name="../evil", display_name="x")


def test_create_skill_invalid_category(manager):
    with pytest.raises(ValueError, match="分类"):
        manager.create_skill(name="s1", display_name="x", category="hack")


def test_create_skill_duplicate(manager):
    manager.create_skill(name="s1", display_name="x")
    with pytest.raises(ValueError, match="已存在"):
        manager.create_skill(name="s1", display_name="x")


def test_update_skill(manager):
    manager.create_skill(name="s1", display_name="旧名", description="旧描述", category="search")
    info = manager.update_skill("s1", display_name="新名", description="新描述", category="writing", skill_md="# 新指令")
    assert info["display_name"] == "新名"
    assert info["description"] == "新描述"
    assert info["category"] == "writing"
    md = (manager._pool_dir() / "s1" / "skill.md").read_text(encoding="utf-8")
    assert "display_name: 新名" in md
    assert "# 新指令" in md


def test_update_skill_builtin_forbidden(manager):
    with pytest.raises(ValueError, match="内置技能不可修改"):
        manager.update_skill("builtin_search", description="x")


def test_update_skill_unknown(manager):
    with pytest.raises(KeyError):
        manager.update_skill("nope", description="x")


def test_delete_skill_custom(manager):
    manager.create_skill(name="s1", display_name="x")
    manager.delete_skill("s1")
    assert reg_mod.registry.get("s1") is None
    assert not (manager._pool_dir() / "s1").exists()


def test_delete_skill_builtin_forbidden(manager):
    with pytest.raises(ValueError, match="内置技能不可删除"):
        manager.delete_skill("builtin_search")


def test_delete_skill_unknown(manager):
    with pytest.raises(KeyError):
        manager.delete_skill("nope")


# ---------------------------------------------------------------- 管理器：开关


def test_skill_default_enabled(manager):
    for skill in reg_mod.registry.get_all():
        assert skill.enabled is True
        assert skill.to_dict()["enabled"] is True


def test_create_skill_enabled_false(manager):
    info = manager.create_skill(name="s1", display_name="x", enabled=False)
    assert info["enabled"] is False
    # 开关状态统一写入 skill_config.json，不写入 skill.md
    cfg = (manager._pool_dir().parent / "skill_config.json").read_text(encoding="utf-8")
    assert '"s1": false' in cfg
    md = (manager._pool_dir() / "s1" / "skill.md").read_text(encoding="utf-8")
    assert "enabled" not in md


def test_update_skill_enabled(manager):
    manager.create_skill(name="s1", display_name="x")
    info = manager.update_skill("s1", enabled=False)
    assert info["enabled"] is False
    assert reg_mod.registry.get("s1").enabled is False


def test_set_enabled_custom_persisted(manager):
    manager.create_skill(name="s1", display_name="x")
    info = manager.set_enabled("s1", False)
    assert info["enabled"] is False
    # 统一写入 skill_config.json，skill.md 不含开关字段
    cfg = (manager._pool_dir().parent / "skill_config.json").read_text(encoding="utf-8")
    assert '"s1": false' in cfg
    md = (manager._pool_dir() / "s1" / "skill.md").read_text(encoding="utf-8")
    assert "enabled" not in md
    # 重启重载（init）后开关状态仍保留
    manager.init()
    assert reg_mod.registry.get("s1").enabled is False


def test_set_enabled_builtin(manager):
    info = manager.set_enabled("builtin_search", False)
    assert info["enabled"] is False
    assert reg_mod.registry.get("builtin_search").enabled is False
    # 内置技能开关持久化到 skill_config.json，重启（init）后保留
    cfg = (manager._pool_dir().parent / "skill_config.json").read_text(encoding="utf-8")
    assert '"builtin_search": false' in cfg
    manager.init()
    assert reg_mod.registry.get("builtin_search").enabled is False


def test_set_enabled_builtin_roundtrip(manager):
    """内置技能开关与自定义技能开关行为统一：重启后均保留。"""
    manager.set_enabled("builtin_write", False)
    manager.create_skill(name="s1", display_name="x")
    manager.set_enabled("s1", False)
    manager.init()
    assert reg_mod.registry.get("builtin_write").enabled is False
    assert reg_mod.registry.get("s1").enabled is False


def test_set_enabled_unknown(manager):
    with pytest.raises(KeyError):
        manager.set_enabled("nope", True)


def test_run_skill_disabled_raises(manager):
    manager.create_skill(name="s1", display_name="x")
    manager.set_enabled("s1", False)
    with pytest.raises(ValueError, match="已禁用"):
        asyncio.run(manager.run_skill("s1", "hi"))


# ---------------------------------------------------------------- 管理器：zip 导入


def test_import_skill_zip(manager):
    md = "---\nname: zip_skill\ndisplay_name: ZIP技能\ndescription: 来自zip\ncategory: review\nversion: 1.0.0\n---\n\n# 指令正文"
    data = _make_zip({"skill.md": md, "assets/data.txt": "hello"})
    info = manager.import_skill(data)
    assert info["name"] == "zip_skill"
    assert info["builtin"] is False
    assert (manager._pool_dir() / "zip_skill" / "skill.md").exists()
    assert (manager._pool_dir() / "zip_skill" / "assets" / "data.txt").exists()
    assert reg_mod.registry.get("zip_skill") is not None


def test_import_skill_claude_format_root(manager):
    """Claude 技能包：SKILL.md（大写）+ references/ + _meta.json，归一化为 skill.md。"""
    md = (
        "---\nname: humanizer\ndescription: \"去除文本中的AI写作痕迹\"\nversion: 4.1.0\n"
        "allowed-tools:\n  - Read\n  - Write\ntrigger: [\"去AI味\"]\n---\n\n# Humanizer\n你是写作编辑。\n"
    )
    data = _make_zip(
        {
            "SKILL.md": md,
            "references/banned-words.md": "foo",
            "_meta.json": '{"slug": "x", "version": "1.0.5"}',
        }
    )
    info = manager.import_skill(data)
    assert info["name"] == "humanizer"
    assert info["display_name"] == "humanizer"
    assert info["version"] == "4.1.0"
    # 大写 SKILL.md 应归一化为小写 skill.md 持久化，避免 update 时产生双文件
    # （Windows 文件系统不区分大小写，exists() 无法区分大小写，需比对实际目录项名）
    names = {p.name for p in (manager._pool_dir() / "humanizer").iterdir() if p.is_file()}
    assert "skill.md" in names
    assert "SKILL.md" not in names
    assert (manager._pool_dir() / "humanizer" / "references" / "banned-words.md").exists()
    assert reg_mod.registry.get("humanizer") is not None
    # 重启重载（init）后仍可加载（_load_skill_dir 兼容大写场景的双保险）
    mgr2 = SkillsManager(pool_dir=manager._pool_dir())
    mgr2.init()
    assert reg_mod.registry.get("humanizer") is not None


def test_import_skill_claude_format_nested(manager):
    """Claude 技能包位于顶层目录内：dir/SKILL.md。"""
    md = "---\nname: nested_skill\ndisplay_name: 嵌套技能\n---\n\n正文"
    data = _make_zip({"some-dir/SKILL.md": md})
    info = manager.import_skill(data)
    assert info["name"] == "nested_skill"
    assert (manager._pool_dir() / "nested_skill" / "skill.md").exists()


def test_import_skill_zip_with_top_folder(manager):
    md = "---\nname: zip_skill2\ndisplay_name: 技能二\ncategory: analysis\n---\n\n正文"
    data = _make_zip({"zip_skill2/skill.md": md})
    info = manager.import_skill(data)
    assert info["name"] == "zip_skill2"
    assert (manager._pool_dir() / "zip_skill2" / "skill.md").exists()


def test_import_skill_zip_with_skill_py(manager, monkeypatch):
    monkeypatch.setenv("SMARTBRIEF_ALLOW_SKILL_PYTHON", "1")
    md = "---\nname: py_skill\ndisplay_name: PY技能\ncategory: writing\n---\n\n正文"
    py = (
        "from skills.base_skill import BaseSkill, SkillInput, SkillOutput\n"
        "\n"
        "class MySkill(BaseSkill):\n"
        "    name = 'py_skill'\n"
        "    display_name = 'PY技能'\n"
        "    category = 'writing'\n"
        "\n"
        "    async def execute(self, input: SkillInput) -> SkillOutput:\n"
        "        return SkillOutput(status='success', data={'custom': input.query})\n"
    )
    data = _make_zip({"skill.md": md, "skill.py": py})
    info = manager.import_skill(data)
    assert info["name"] == "py_skill"
    assert info["builtin"] is False  # 导入一律按自定义
    out = asyncio.run(manager.run_skill("py_skill", "hi"))
    assert out.data == {"custom": "hi"}


def test_import_skill_missing_skill_md(manager):
    data = _make_zip({"readme.txt": "hi"})
    with pytest.raises(ValueError, match="未找到 skill.md"):
        manager.import_skill(data)


def test_import_skill_invalid_zip(manager):
    with pytest.raises(ValueError, match="zip"):
        manager.import_skill(b"not a zip")


def test_import_skill_path_traversal(manager):
    md = "---\nname: evil\ndisplay_name: 坏\ncategory: search\n---\n\n正文"
    data = _make_zip({"skill.md": md, "../../evil.txt": "x"})
    with pytest.raises(ValueError, match="非法路径"):
        manager.import_skill(data)


def test_import_skill_too_large(manager, monkeypatch):
    from skills import skills_manager as sm_mod

    monkeypatch.setattr(sm_mod, "MAX_ZIP_SIZE", 10)
    data = _make_zip({"skill.md": "x" * 100})
    with pytest.raises(ValueError, match="过大"):
        manager.import_skill(data)


def test_import_skill_duplicate(manager):
    manager.create_skill(name="s1", display_name="x")
    md = "---\nname: s1\ndisplay_name: x\ncategory: search\n---\n\n正文"
    with pytest.raises(ValueError, match="已存在"):
        manager.import_skill(_make_zip({"skill.md": md}))


# ---------------------------------------------------------------- 管理器：执行


def test_run_builtin_skill(manager, monkeypatch):
    async def fake_call_tool(name, arguments=None):
        return {"source": "test", "query": "储能市场", "results": [{"title": "t", "url": "u"}]}

    monkeypatch.setattr("skills.builtin_skills.mcp_manager", SimpleNamespace(call_tool=fake_call_tool))
    out = asyncio.run(manager.run_skill("builtin_search", "储能市场"))
    assert out.status == "success"
    assert out.data["query"] == "储能市场"
    assert out.data["results"] == [{"title": "t", "url": "u"}]


def test_run_custom_markdown_skill_uses_llm(manager, monkeypatch):
    manager.create_skill(name="s1", display_name="技能", skill_md="# 指令")

    async def fake_chat(messages, tools=None, tool_choice=None):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="LLM 输出", tool_calls=None))]
        )

    monkeypatch.setattr(llm_client, "chat", fake_chat)
    out = asyncio.run(manager.run_skill("s1", "hello"))
    assert out.status == "success"
    assert out.data["reply"] == "LLM 输出"


def test_run_custom_markdown_skill_llm_error(manager, monkeypatch):
    manager.create_skill(name="s1", display_name="技能", skill_md="# 指令")

    async def fail_chat(messages, tools=None, tool_choice=None):
        raise RuntimeError("网络错误")

    monkeypatch.setattr(llm_client, "chat", fail_chat)
    out = asyncio.run(manager.run_skill("s1", "hello"))
    assert out.status == "failed"
    assert "网络错误" in out.metadata["error"]


def test_run_skill_unknown(manager):
    with pytest.raises(KeyError):
        asyncio.run(manager.run_skill("nope", "hi"))


# ---------------------------------------------------------------- skill.md 解析


def test_parse_skill_md():
    meta, body = _parse_skill_md(
        "---\nname: a\ndisplay_name: 甲\ncategory: search\n---\n\n# 正文"
    )
    assert meta["name"] == "a"
    assert meta["category"] == "search"
    assert body == "# 正文"


def test_parse_skill_md_without_frontmatter():
    meta, body = _parse_skill_md("# 只有正文")
    assert meta == {}
    assert body == "# 只有正文"


def test_parse_skill_md_unquotes_values():
    """Claude 技能包引号值剥离：description: "..." → 无引号。"""
    meta, _ = _parse_skill_md(
        "---\nname: a\ndescription: \"去除AI味，'不是而是'修复\"\nversion: 4.1.0\n---\n\n正文"
    )
    assert meta["description"] == "去除AI味，'不是而是'修复"


def test_parse_skill_md_multiline_block():
    """多行块值（YAML | 保留换行）：description: | 后续缩进行整体作为值。"""
    meta, _ = _parse_skill_md(
        "---\nname: a\ndescription: |\n  第一行\n  第二行\nversion: 1.0.0\n---\n\n正文"
    )
    assert meta["description"] == "第一行\n第二行"


def test_parse_skill_md_multiline_folded():
    """多行折叠值（YAML > 折叠空格）：description: > 换行折叠为空格。"""
    meta, _ = _parse_skill_md(
        "---\nname: a\ndescription: >\n  第一行\n  第二行\ncategory: search\n---\n\n正文"
    )
    assert meta["description"] == "第一行 第二行"


def test_parse_skill_md_indented_continuation():
    """隐式续行：缩进的纯文本行归属上一个键并折叠拼接。"""
    meta, _ = _parse_skill_md(
        "---\nname: a\ndescription: 开头\n  续行\ncategory: search\n---\n\n正文"
    )
    assert meta["description"] == "开头 续行"


# ---------------------------------------------------------------- references / scripts（Claude 技能包资源）


def _make_skill_dir(tmp_path, name="res_skill", references=None, scripts=None):
    """构造一个带 references/ 与 scripts/ 资源的技能目录。"""
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True)
    if references:
        (skill_dir / "references").mkdir()
        for fn, content in references.items():
            (skill_dir / "references" / fn).write_text(content, encoding="utf-8")
    if scripts:
        (skill_dir / "scripts").mkdir()
        for fn, content in scripts.items():
            (skill_dir / "scripts" / fn).write_text(content, encoding="utf-8")
    return skill_dir


def test_markdown_skill_loads_references(tmp_path):
    """references/*.md 被扫描进 MarkdownSkill.references，且注入 system prompt。"""
    skill_dir = _make_skill_dir(tmp_path, references={"banned-words.md": "禁用词：但是、然而"})
    skill = MarkdownSkill({"name": "res_skill", "display_name": "资源技能"}, "# 指令", skill_dir=skill_dir)
    assert [name for name, _ in skill.references] == ["banned-words.md"]
    system = skill._build_system()
    assert "禁用词：但是、然而" in system


def test_markdown_skill_without_dir_no_resources(tmp_path):
    """不传 skill_dir 时不扫描资源（保持原行为）。"""
    skill = MarkdownSkill({"name": "plain"}, "# 指令")
    assert skill.references == []
    assert skill.scripts == []
    assert "【技能参考文档" not in skill._build_system()


def test_markdown_skill_discover_scripts(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTBRIEF_ALLOW_SKILL_SCRIPTS", "1")
    """scripts/ 目录下的脚本名被扫描（白名单过滤）。"""
    skill_dir = _make_skill_dir(tmp_path, scripts={"tool.py": "print('ok')", "bad name.sh": "echo x"})
    skill = MarkdownSkill({"name": "s"}, "body", skill_dir=skill_dir)
    assert skill.scripts == ["tool.py"]  # "bad name.sh" 含空格，被白名单过滤


def test_run_script_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTBRIEF_ALLOW_SKILL_SCRIPTS", "1")
    """run_script 正常执行：返回脚本 stdout。"""
    skill_dir = _make_skill_dir(tmp_path, scripts={"hello.py": "print('hello')"})
    skill = MarkdownSkill({"name": "s"}, "body", skill_dir=skill_dir)
    out = asyncio.run(skill._run_script("hello.py"))
    assert out == "hello"


def test_run_script_rejects_unregistered(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTBRIEF_ALLOW_SKILL_SCRIPTS", "1")
    """白名单拒绝：未登记的脚本名直接报错。"""
    skill_dir = _make_skill_dir(tmp_path, scripts={"hello.py": "print('hello')"})
    skill = MarkdownSkill({"name": "s"}, "body", skill_dir=skill_dir)
    with pytest.raises(ValueError, match="不允许执行未登记的脚本"):
        asyncio.run(skill._run_script("../evil.py"))
    with pytest.raises(ValueError, match="不允许执行未登记的脚本"):
        asyncio.run(skill._run_script("C:/Windows/System32/calc.exe"))


def test_run_script_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTBRIEF_ALLOW_SKILL_SCRIPTS", "1")
    """超时强杀：脚本执行超过 SCRIPT_TIMEOUT_SECONDS 被终止并报错。"""
    import skills.skills_manager as sm_mod

    monkeypatch.setattr(sm_mod, "SCRIPT_TIMEOUT_SECONDS", 1)
    skill_dir = _make_skill_dir(tmp_path, scripts={"slow.py": "import time; time.sleep(30)"})
    skill = MarkdownSkill({"name": "s"}, "body", skill_dir=skill_dir)
    with pytest.raises(ValueError, match="脚本执行超时"):
        asyncio.run(skill._run_script("slow.py"))


def test_run_script_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTBRIEF_ALLOW_SKILL_SCRIPTS", "1")
    """非零退出码：捕获 stderr 并报错。"""
    skill_dir = _make_skill_dir(tmp_path, scripts={"fail.py": "import sys; sys.exit(3)"})
    skill = MarkdownSkill({"name": "s"}, "body", skill_dir=skill_dir)
    with pytest.raises(ValueError, match="脚本执行失败"):
        asyncio.run(skill._run_script("fail.py"))


def test_scripts_execute_tool_loop(manager, monkeypatch):
    monkeypatch.setenv("SMARTBRIEF_ALLOW_SKILL_SCRIPTS", "1")
    """带 scripts/ 的技能：execute 走工具循环（run_script → 回填 → 最终回答）。"""

    skill_dir = _make_skill_dir(manager._pool_dir(), name="tool_skill", scripts={"calc.py": "print('42')"})
    skill = MarkdownSkill({"name": "tool_skill", "display_name": "工具技能"}, "# 用脚本算", skill_dir=skill_dir)
    from skills import registry as reg
    reg.registry.register(skill)

    calls = []

    async def fake_chat(messages, tools=None, tool_choice=None):
        calls.append({"tools": tools, "len": len(messages)})
        if len(messages) == 2:  # 第一次：模型要求跑脚本
            call = SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(name="run_script", arguments='{"script": "calc.py"}'),
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]))])
        # 第二次：工具结果已回填，模型给出最终回答
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="结果是 42", tool_calls=None))])

    monkeypatch.setattr(llm_client, "chat", fake_chat)
    out = asyncio.run(manager.run_skill("tool_skill", "帮我算"))
    assert out.status == "success"
    assert out.data["reply"] == "结果是 42"
    assert out.metadata["scripts"] == [{"script": "calc.py", "ok": True}]
    # 工具循环必须带 tools 注入（run_script schema）
    assert calls[0]["tools"] == [skill._script_schema()]


def test_scripts_execute_max_loop(manager, monkeypatch):
    monkeypatch.setenv("SMARTBRIEF_ALLOW_SKILL_SCRIPTS", "1")
    """达到 SCRIPT_MAX_LOOP 上限仍未给最终回答 → 返回兜底提示。"""

    skill_dir = _make_skill_dir(manager._pool_dir(), name="loop_skill", scripts={"a.py": "print('a')"})
    skill = MarkdownSkill({"name": "loop_skill", "display_name": "循环技能"}, "body", skill_dir=skill_dir)
    from skills import registry as reg
    reg.registry.register(skill)

    async def fake_chat(messages, tools=None, tool_choice=None):
        call = SimpleNamespace(
            id="call_x",
            function=SimpleNamespace(name="run_script", arguments='{"script": "a.py"}'),
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]))])

    monkeypatch.setattr(llm_client, "chat", fake_chat)
    out = asyncio.run(manager.run_skill("loop_skill", "hi"))
    assert out.status == "success"
    assert "最大脚本调用轮次" in out.data["reply"]
