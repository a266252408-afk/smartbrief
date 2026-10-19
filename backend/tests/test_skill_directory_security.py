"""启动加载的技能metadata同样不能控制文件系统路径。"""

import pytest
from skills.skills_manager import SkillsManager


@pytest.mark.parametrize("name", ["..", "../outside", "other", "CON"])
def test_startup_does_not_register_untrusted_directory_names(tmp_path, name):
    pool = tmp_path / "pool"
    directory = pool / "safe"
    directory.mkdir(parents=True)
    (directory / "skill.md").write_text(
        f"---\nname: {name}\n---\nfixture", encoding="utf-8"
    )
    marker = tmp_path / "preserved"
    marker.write_text("fixture")
    manager = SkillsManager(pool)
    manager.init()
    assert manager.get_skill(name) is None
    assert marker.read_text() == "fixture"


def test_existing_unregistered_directory_is_not_overwritten(tmp_path):
    pool = tmp_path / "pool"
    directory = pool / "safe"
    directory.mkdir(parents=True)
    marker = directory / "preserved"
    marker.write_text("fixture")
    with pytest.raises(ValueError, match="目录已存在"):
        SkillsManager(pool).create_skill("safe", "固定测试")
    assert marker.read_text() == "fixture"


def test_valid_directory_can_load_and_reserved_create_is_rejected(tmp_path):
    pool = tmp_path / "pool"
    directory = pool / "safe"
    directory.mkdir(parents=True)
    (directory / "skill.md").write_text("---\nname: safe\n---\nfixture")
    manager = SkillsManager(pool)
    manager.init()
    assert manager.get_skill("safe").name == "safe"
    with pytest.raises(ValueError):
        manager.create_skill("CON", "保留名")
    with pytest.raises(ValueError):
        manager._safe_skill_dir("../outside")


def test_loader_rejects_outside_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "skill.md").write_text("---\nname: outside\n---\nfixture")
    with pytest.raises(ValueError, match="目录不一致"):
        SkillsManager(tmp_path / "pool")._load_skill_dir(outside)


def test_linked_directory_cannot_escape_pool(tmp_path):
    import os
    import subprocess

    pool = tmp_path / "pool"
    pool.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "preserved"
    marker.write_text("fixture")
    link = pool / "linked"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="越界|链接"):
            SkillsManager(pool)._safe_skill_dir("linked")
        assert marker.read_text() == "fixture"
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()
