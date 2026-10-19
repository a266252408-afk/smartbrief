"""技能抽象层：SkillInput / SkillOutput / BaseSkill。

技能（Skill）是比"工具（Tool）"更高一层的"能力单元"：
- 工具：单一函数（current_time / calculator / web_search），带 OpenAI 格式 Schema
- 技能：可被智能体调用的完整能力，可组合工具、指令与代码（skill.md + 可选 skill.py）
本模块定义抽象与注册接口，具体实现见 builtin_skills（内置）与自定义技能。
"""
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class SkillInput(BaseModel):
    """技能输入：用户请求 + 可选上下文。"""

    query: str = Field(..., min_length=1, max_length=2000, description="用户请求")
    context: dict = Field(default_factory=dict, description="上下文信息（可选）")


class SkillOutput(BaseModel):
    """技能输出：统一 status / data / metadata。"""

    status: str = Field(..., pattern="^(success|failed)$", description="执行结果")
    data: Any = Field(default=None, description="结果数据")
    metadata: dict = Field(default_factory=dict, description="元信息")


class BaseSkill(ABC):
    """技能基类：所有技能（内置 / 自定义）必须继承并实现 execute()。"""

    name: str = ""
    display_name: str = ""
    description: str = ""
    category: str = ""       # search | analysis | writing | review
    version: str = "1.0.0"
    builtin: bool = False    # 内置技能不可删除 / 修改
    enabled: bool = True     # 开关：false 时不可试运行 / 被智能体调用

    @abstractmethod
    async def execute(self, input: SkillInput) -> SkillOutput:
        """执行技能，返回统一输出。"""
        raise NotImplementedError

    def to_dict(self) -> dict:
        """序列化给 API / 前端展示。"""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "category": self.category,
            "version": self.version,
            "builtin": self.builtin,
            "enabled": self.enabled,
            "source": "builtin" if self.builtin else "custom",
            "skill_md": getattr(self, "skill_md", ""),
        }
