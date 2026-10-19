"""代码信任开关仅来自所有者的后端环境，默认关闭。"""

import os


def python_enabled():
    return os.getenv("SMARTBRIEF_ALLOW_SKILL_PYTHON", "").strip().lower() in {
        "1",
        "true",
    }


def scripts_enabled():
    return os.getenv("SMARTBRIEF_ALLOW_SKILL_SCRIPTS", "").strip().lower() in {
        "1",
        "true",
    }


def require_python():
    if not python_enabled():
        raise ValueError("自定义Python技能执行已禁用")


def require_scripts():
    if not scripts_enabled():
        raise ValueError("自定义脚本执行已禁用")
