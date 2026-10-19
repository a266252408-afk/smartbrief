"""内置工具（Function Calling）：current_time / calculator + 注册表管理。

每个内置工具 = 一个 Python 函数 + 一个 OpenAI 格式的 JSON Schema。
注册表统一管理工具的开关状态（enabled），持久化到 tool_config.json：
关闭的工具不会注入 LLM 的 tools 参数，但仍在注册表中可查、可重新开启。
"""
import ast
import json
import operator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 配置持久化

CONFIG_PATH = Path(__file__).resolve().parents[1] / "tool_config.json"


def _load_config() -> dict:
    """读取工具开关配置（默认全部开启）。"""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"builtin": {}}


def _save_config() -> None:
    """保存工具开关配置。"""
    config = {"builtin": {name: tool.enabled for name, tool in _TOOLS.items()}}
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------- 工具定义

@dataclass
class BuiltinTool:
    """内置工具描述：名称 / 描述 / OpenAI Schema / 执行函数 / 开关状态。"""

    name: str
    description: str
    schema: dict
    handler: object  # Callable[..., Awaitable[str] | str]
    enabled: bool = True
    metadata: dict = field(default_factory=dict)  # 扩展信息（分类、参数说明等）


def current_time_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "current_time",
            "description": "获取服务器当前日期和时间（格式：YYYY-MM-DD HH:MM:SS）",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


async def current_time() -> str:
    """返回当前时间字符串。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 四则运算白名单（安全求值）
# 实现说明：绝不直接使用 eval() / exec() 执行用户输入的表达式——它们能执行任意 Python 代码
# （如 __import__("os").system("...")），是严重的安全漏洞。正确做法是：
#   1) ast.parse() 把表达式解析成"抽象语法树"（树形结构，不执行任何代码）
#   2) 递归遍历树的节点，只放行"我们点过名的节点类型"（白名单）
#   3) 白名单之外的任何节点（函数调用 Calls、属性访问 Attribute、下标 Subscript…）→ 直接拒绝
_BINARY_OPS = {
    ast.Add: operator.add,   # + 加
    ast.Sub: operator.sub,   # - 减
    ast.Mult: operator.mul,  # * 乘
    ast.Div: operator.truediv,  # / 除（真除法，保留小数）
    ast.Mod: operator.mod,   # % 取余
    ast.Pow: operator.pow,   # ** 幂
}


def _safe_eval(node: ast.AST) -> float:
    """安全求值器：递归遍历 AST 节点，只允许数字、括号与白名单内的四则运算。

    执行流程：
    - Expression（表达式根节点）→ 取其 body 继续递归
    - Constant（字面量）→ 只放行 int / float 数字
    - BinOp（二元运算）→ 运算符必须命中 _BINARY_OPS 白名单，再递归求左右操作数
    - UnaryOp（一元运算）→ 只放行负数（-x）
    - 其余一切节点（函数调用、属性访问、列表下标等）→ 抛错拒绝
    """
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)                    # 脱掉表达式外壳，进入真正的表达式体
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value                               # 数字字面量（如 1、2.5）直接返回
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
        left = _safe_eval(node.left)                    # 递归求值左操作数
        right = _safe_eval(node.right)                  # 递归求值右操作数
        return _BINARY_OPS[type(node.op)](left, right)  # 用白名单映射的操作符执行运算
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_safe_eval(node.operand)                # 支持负号（如 -5、(2-7)）
    raise ValueError("仅支持数字、四则运算与括号")        # 出现未白名单节点 → 拒绝


def calculator_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "执行四则运算，支持加(+)、减(-)、乘(*)、除(/)、括号，例如 '1 + 2 * 3'",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式，例如 '1 + 2 * 3'",
                    },
                },
                "required": ["expression"],
            },
        },
    }


async def calculator(expression: str) -> str:
    """安全计算四则运算表达式。"""
    try:
        tree = ast.parse(expression, mode="eval")
        value = _safe_eval(tree)
        return str(value)
    except (ValueError, SyntaxError, ZeroDivisionError) as exc:
        return f"计算错误：{exc}"


# ---------------------------------------------------------------- 注册表

_TOOLS: dict[str, BuiltinTool] = {}


def _register(tool: BuiltinTool) -> None:
    _TOOLS[tool.name] = tool


def init() -> None:
    """初始化注册表：注册全部内置工具，并应用持久化的开关状态。"""
    _TOOLS.clear()
    _register(
        BuiltinTool(
            name="current_time",
            description="获取服务器当前日期和时间",
            schema=current_time_schema(),
            handler=current_time,
            metadata={"category": "system", "params": "无参数"},
        )
    )
    _register(
        BuiltinTool(
            name="calculator",
            description="执行四则运算（加/减/乘/除/括号）",
            schema=calculator_schema(),
            handler=calculator,
            metadata={"category": "math", "params": "expression: 数学表达式字符串"},
        )
    )
    config = _load_config()
    for name, enabled in config.get("builtin", {}).items():
        if name in _TOOLS:
            _TOOLS[name].enabled = bool(enabled)


def list_tools() -> list[dict]:
    """内置工具列表（供 API / 前端展示，含开关状态）。"""
    return [
        {
            "name": tool.name,
            "type": "builtin",
            "description": tool.description,
            "enabled": tool.enabled,
            "metadata": tool.metadata,
        }
        for tool in _TOOLS.values()
    ]


def list_enabled_schemas() -> list[dict]:
    """已开启工具的 Schema 列表（注入 LLM tools 参数）。"""
    return [tool.schema for tool in _TOOLS.values() if tool.enabled]


def get_tool(name: str) -> BuiltinTool | None:
    """按名称获取内置工具。"""
    return _TOOLS.get(name)


def set_enabled(name: str, enabled: bool) -> BuiltinTool | None:
    """设置工具开关，返回更新后的工具（未知名称返回 None）。"""
    tool = _TOOLS.get(name)
    if tool is None:
        return None
    tool.enabled = bool(enabled)
    _save_config()
    return tool
