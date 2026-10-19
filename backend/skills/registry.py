"""技能注册表：统一管理技能的注册 / 查询 / 更新 / 删除。

规则：
- 内置技能（builtin=True）不可删除 / 不可修改，违反时抛 ValueError（API 层转 400）
- 自定义技能（builtin=False）可注册、更新、删除
"""
from skills.base_skill import BaseSkill


class SkillRegistry:
    """技能注册表：name → BaseSkill。"""

    def __init__(self) -> None:
        self._skills: dict[str, BaseSkill] = {}

    def clear(self) -> None:
        """清空注册表（重新初始化时使用）。"""
        self._skills.clear()

    def register(self, skill: BaseSkill) -> None:
        """注册技能（重名抛错）。"""
        if not skill.name:
            raise ValueError("技能 name 不能为空")
        if skill.name in self._skills:
            raise ValueError(f"技能已存在：{skill.name}")
        self._skills[skill.name] = skill

    def unregister(self, name: str) -> None:
        """注销技能（内置技能抛错，禁止删除）。"""
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(f"技能不存在：{name}")
        if skill.builtin:
            raise ValueError(f"内置技能不可删除：{name}")
        del self._skills[name]

    def get(self, name: str) -> BaseSkill | None:
        """按名称获取技能。"""
        return self._skills.get(name)

    def get_all(self) -> list[BaseSkill]:
        """全部技能列表。"""
        return list(self._skills.values())

    def get_source(self, name: str) -> str:
        """技能来源：builtin / custom / ""（不存在）。"""
        skill = self._skills.get(name)
        if skill is None:
            return ""
        return "builtin" if skill.builtin else "custom"

    def set_enabled(self, name: str, enabled: bool) -> BaseSkill:
        """开关技能（内置 / 自定义均可）；不存在抛 KeyError。"""
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(f"技能不存在：{name}")
        skill.enabled = bool(enabled)
        return skill


registry = SkillRegistry()
