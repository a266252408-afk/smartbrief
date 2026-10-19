"""内置工具模块测试（tools/builtin_tools.py）。"""
import asyncio
import ast
import json
import re
from datetime import datetime

import pytest

from tools import builtin_tools


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """将配置持久化路径重定向到临时目录。"""
    cfg = tmp_path / "tool_config.json"
    monkeypatch.setattr(builtin_tools, "CONFIG_PATH", cfg)
    return cfg


# ---------------------------------------------------------------- 注册表


def test_init_registers_all_tools(tmp_config):
    builtin_tools.init()
    tools = builtin_tools.list_tools()
    assert [t["name"] for t in tools] == ["current_time", "calculator"]
    assert all(t["enabled"] for t in tools)


def test_init_applies_persisted_state(tmp_config):
    builtin_tools.init()
    builtin_tools.set_enabled("calculator", False)
    # 重新 init 应读取持久化状态
    builtin_tools.init()
    assert builtin_tools.get_tool("calculator").enabled is False
    assert builtin_tools.get_tool("current_time").enabled is True


def test_list_enabled_schemas(tmp_config):
    builtin_tools.init()
    schemas = builtin_tools.list_enabled_schemas()
    assert len(schemas) == 2
    names = {s["function"]["name"] for s in schemas}
    assert names == {"current_time", "calculator"}


def test_list_enabled_schemas_after_disable(tmp_config):
    builtin_tools.init()
    builtin_tools.set_enabled("current_time", False)
    schemas = builtin_tools.list_enabled_schemas()
    assert [s["function"]["name"] for s in schemas] == ["calculator"]


def test_get_tool_unknown_returns_none(tmp_config):
    builtin_tools.init()
    assert builtin_tools.get_tool("not_exist") is None


def test_set_enabled_unknown_returns_none(tmp_config):
    builtin_tools.init()
    assert builtin_tools.set_enabled("not_exist", True) is None


def test_set_enabled_persists_to_file(tmp_config):
    builtin_tools.init()
    builtin_tools.set_enabled("current_time", False)
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert data["builtin"]["current_time"] is False
    assert data["builtin"]["calculator"] is True


def test_load_config_corrupted_file(tmp_config):
    tmp_config.write_text("{ not json", encoding="utf-8")
    assert builtin_tools._load_config() == {"builtin": {}}


def test_load_config_missing_file(tmp_config):
    assert builtin_tools._load_config() == {"builtin": {}}


# ---------------------------------------------------------------- current_time


def test_current_time():
    result = asyncio.run(builtin_tools.current_time())
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", result)


def test_current_time_schema():
    schema = builtin_tools.current_time_schema()
    assert schema["function"]["name"] == "current_time"


# ---------------------------------------------------------------- calculator


def test_calculator_basic():
    assert asyncio.run(builtin_tools.calculator("1 + 2 * 3")) == "7"


def test_calculator_parens():
    assert asyncio.run(builtin_tools.calculator("(1 + 2) * 3")) == "9"


def test_calculator_unary_minus():
    assert asyncio.run(builtin_tools.calculator("-5 + 3")) == "-2"


def test_calculator_zero_division():
    assert asyncio.run(builtin_tools.calculator("1 / 0")) == "计算错误：division by zero"


def test_calculator_syntax_error():
    result = asyncio.run(builtin_tools.calculator("1 +"))
    # 不同 Python 版本 SyntaxError 消息不同，仅断言错误前缀 + 语法错误特征
    assert result.startswith("计算错误：")
    assert ("unexpected EOF" in result) or ("invalid syntax" in result)


def test_calculator_invalid_expression():
    assert asyncio.run(builtin_tools.calculator("x + 1")) == "计算错误：仅支持数字、四则运算与括号"


def test_calculator_schema():
    schema = builtin_tools.calculator_schema()
    assert schema["function"]["name"] == "calculator"
    assert "expression" in schema["function"]["parameters"]["properties"]


# ---------------------------------------------------------------- _safe_eval


def test_safe_eval_expression_wrapper():
    tree = ast.parse("3 + 4", mode="eval")
    assert builtin_tools._safe_eval(tree) == 7


def test_safe_eval_float():
    assert builtin_tools._safe_eval(ast.Constant(value=3.5)) == 3.5


def test_safe_eval_pow_and_mod():
    tree = ast.parse("2 ** 3 % 3", mode="eval")
    assert builtin_tools._safe_eval(tree) == 2


def test_safe_eval_invalid_node_raises():
    tree = ast.parse("x + 1", mode="eval")
    with pytest.raises(ValueError):
        builtin_tools._safe_eval(tree)
