"""技能管理器：自定义技能的全生命周期管理。

- 持久化目录：backend/skills_pool/<name>/
  - skill.md（必需）：YAML frontmatter（name/display_name/description/category/version）+ Markdown 正文（使用说明）
  - skill.py（可选）：继承 BaseSkill 的可执行实现；缺失时由 LLM 按 skill.md 指令执行
- 启动时（init）：注册 4 个内置技能 + 扫描 skills_pool/ 重新加载全部自定义技能
- 提供 create / update / delete / import_zip / run，供 API 层调用
"""
import asyncio
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from uuid import uuid4

from llm import client as llm_client
from services.file_security import validate_archive
from skills import registry as _registry
from skills.base_skill import BaseSkill, SkillInput, SkillOutput
from skills.builtin_skills import BUILTIN_SKILLS

SKILLS_POOL_DIR = Path(__file__).resolve().parents[1] / "skills_pool"
SKILL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "skill_config.json"
MAX_ZIP_SIZE = 2 * 1024 * 1024  # 单个 zip 上限 2MB

VALID_CATEGORIES = ("search", "analysis", "writing", "review", "other")
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# scripts/ 目录下的脚本名白名单：字母/数字开头，可含 . _ -，最长 64 位
SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SCRIPT_TIMEOUT_SECONDS = 30  # 单个脚本执行超时上限（秒）
SCRIPT_MAX_LOOP = 5          # 单次技能执行内允许的最多脚本调用轮数


DEFAULT_SKILL_BODY = """## 技能说明

请根据用户的请求执行本技能的任务，并以 Markdown 输出结构化结果。
"""


def _load_skill_config() -> dict:
    """读取技能开关配置（skill_config.json），返回 {"skills": {name: enabled}}。"""
    if SKILL_CONFIG_PATH.exists():
        try:
            return json.loads(SKILL_CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_skill_config() -> None:
    """持久化全部技能开关状态（内置 + 自定义统一写入 skill_config.json，与 tool_config.json 模式一致）。"""
    skills = {skill.name: skill.enabled for skill in _registry.registry.get_all()}
    SKILL_CONFIG_PATH.write_text(
        json.dumps({"skills": skills}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class MarkdownSkill(BaseSkill):
    """skill.md 驱动的自定义技能：无 skill.py 时由 LLM 按说明执行。

    Claude 技能格式的资源支持：
    - references/（可选）：参考文档，构造时读入内存，execute 时拼进 system prompt；
    - scripts/（可选）：可执行脚本，注册为 run_script 工具（enum 白名单），走工具循环。
    """

    def __init__(self, meta: dict, body: str, skill_dir: Path | None = None) -> None:
        self.name = str(meta.get("name") or "")
        self.display_name = str(meta.get("display_name") or self.name)
        self.description = str(meta.get("description") or "")
        self.category = str(meta.get("category") or "analysis")
        self.version = str(meta.get("version") or "1.0.0")
        self.builtin = False
        self.enabled = str(meta.get("enabled", "true")).strip().lower() != "false"
        self.skill_md = body
        # 资源加载：references/（注入 prompt）与 scripts/（工具清单）
        self.references: list[tuple[str, str]] = []
        self.scripts: list[str] = []
        self._skill_dir = skill_dir
        if skill_dir is not None:
            self._load_resources(skill_dir)

    def _load_resources(self, skill_dir: Path) -> None:
        """扫描技能目录资源：references/*.md（注入 prompt）与 scripts/*（工具清单）。"""
        ref_dir = skill_dir / "references"
        if ref_dir.is_dir():
            for md_file in sorted(ref_dir.glob("*.md")):
                self.references.append((md_file.name, _read_text_fallback(md_file)))
        scripts_dir = skill_dir / "scripts"
        if scripts_dir.is_dir():
            self.scripts = sorted(
                p.name for p in scripts_dir.iterdir()
                if p.is_file() and SCRIPT_NAME_RE.match(p.name)
            )

    def _build_system(self) -> str:
        """组装 system 人设：skill.md 正文 + references 注入（Claude 兼容）。"""
        system = f"你是技能「{self.display_name}」，请严格按照以下说明执行：\n\n{self.skill_md}"
        if self.references:
            ref_text = "\n\n".join(
                f"### 参考资料：{name}\n\n{content}" for name, content in self.references
            )
            system += f"\n\n【技能参考文档（references/，必须按需查阅）】\n{ref_text}"
        return system

    def _script_schema(self) -> dict:
        """run_script 工具的 OpenAI schema：script 参数用 enum 白名单限定脚本名。"""
        return {
            "type": "function",
            "function": {
                "name": "run_script",
                "description": f"执行本技能 scripts/ 目录下的脚本并返回其标准输出。可用脚本：{', '.join(self.scripts)}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "script": {
                            "type": "string",
                            "enum": self.scripts,
                            "description": "要执行的脚本文件名（仅限列表内的白名单脚本）",
                        },
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "传给脚本的命令行参数（可选）",
                        },
                    },
                    "required": ["script"],
                },
            },
        }

    async def _run_script(self, script: str, args: list[str] | None = None) -> str:
        """执行 scripts/ 下的脚本，返回 stdout（安全三防线：白名单 + 禁 shell + 超时强杀）。"""
        if script not in self.scripts:
            raise ValueError(f"不允许执行未登记的脚本：{script}")
        script_path = self._skill_dir / "scripts" / script
        if not script_path.is_file():
            raise ValueError(f"脚本不存在：{script}")
        # .py 脚本用当前解释器执行；其余按可执行文件直接运行（均为 argv 直传，禁 shell）
        command = [sys.executable, str(script_path), *(args or [])] if script.endswith(".py") else [str(script_path), *(args or [])]
        # 用 subprocess.run 放到线程池执行：Windows 下 Jupyter 的 SelectorEventLoop
        # 不支持 asyncio 子进程（NotImplementedError），to_thread 两种环境都兼容。
        def _run() -> subprocess.CompletedProcess:
            return subprocess.run(
                command,
                cwd=str(self._skill_dir / "scripts"),
                capture_output=True,
                text=True,
                timeout=SCRIPT_TIMEOUT_SECONDS,
            )

        try:
            proc = await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"脚本执行超时（>{SCRIPT_TIMEOUT_SECONDS}s）：{script}") from exc
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            raise ValueError(f"脚本执行失败（exit={proc.returncode}）：{err or '无错误输出'}")
        return (proc.stdout or "").strip()

    async def execute(self, input: SkillInput) -> SkillOutput:
        """由 LLM 按 skill.md 指令执行用户请求（带 scripts/ 时走工具循环）。"""
        system = self._build_system()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": input.query},
        ]
        if not self.scripts:
            return await self._execute_plain(messages)
        return await self._execute_with_scripts(messages)

    async def _execute_plain(self, messages: list) -> SkillOutput:
        """无脚本技能：单次 chat。"""
        try:
            resp = await llm_client.chat(messages)
            reply = (resp.choices[0].message.content or "").strip()
            return SkillOutput(status="success", data={"reply": reply}, metadata={"mode": "llm"})
        except Exception as exc:
            return SkillOutput(status="failed", data=None, metadata={"error": str(exc), "mode": "llm"})

    async def _execute_with_scripts(self, messages: list) -> SkillOutput:
        """带 scripts/ 的技能：Function Calling 工具循环（run_script 调用闭环）。"""
        tools = [self._script_schema()]
        script_calls: list = []
        try:
            for _ in range(SCRIPT_MAX_LOOP):
                resp = await llm_client.chat(messages, tools=tools)
                message = resp.choices[0].message
                if not message.tool_calls:
                    # 模型给出最终回答 → 结束循环
                    return SkillOutput(
                        status="success",
                        data={"reply": message.content or ""},
                        metadata={"mode": "llm", "scripts": script_calls},
                    )
                # OpenAI 规范：tool 消息前必须回填 assistant.tool_calls
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.function.name,
                                    "arguments": call.function.arguments,
                                },
                            }
                            for call in message.tool_calls
                        ],
                    }
                )
                for call in message.tool_calls:
                    name = call.function.name
                    arguments = json.loads(call.function.arguments or "{}")
                    try:
                        if name != "run_script":
                            raise ValueError(f"技能不支持的工具：{name}")
                        result = await self._run_script(arguments.get("script", ""), arguments.get("args") or [])
                        script_calls.append({"script": arguments.get("script"), "ok": True})
                    except Exception as exc:
                        result = {"error": str(exc)}
                        script_calls.append({"script": arguments.get("script"), "ok": False})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
        except Exception as exc:
            return SkillOutput(status="failed", data=None, metadata={"error": str(exc), "mode": "llm", "scripts": script_calls})
        # 达到 SCRIPT_MAX_LOOP 仍未给出最终回答
        return SkillOutput(
            status="success",
            data={"reply": "已达到最大脚本调用轮次，请尝试简化问题后重试。"},
            metadata={"mode": "llm", "scripts": script_calls},
        )


# ---------------------------------------------------------------- skill.md 解析 / 生成


def _clean(value: str) -> str:
    """frontmatter 单行值清洗：换行折叠为空格。"""
    return " ".join(str(value).splitlines()).strip()


def _unquote(value: str) -> str:
    """剥离 frontmatter 值首尾的成对引号（兼容 Claude SKILL.md 的 YAML 引号写法）。"""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


# 读取技能文档时的编码容错：
# Windows 下用户可能用记事本（ANSI/GBK）或其它非 UTF-8 工具保存 skill.md，
# 直接 utf-8 解码会抛 UnicodeDecodeError（导入失败、显示晦涩的英文报错）。
# 统一按 utf-8 → gb18030（GBK 超集，覆盖简体中文）→ latin-1（永不失败）依次解码。
def _read_text_fallback(path: Path) -> str:
    """读取文本文件，兼容 UTF-8 / GBK 等常见编码。"""
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _parse_skill_md(text: str) -> tuple[dict, str]:
    """解析 skill.md：返回 (frontmatter 元数据, Markdown 正文)。

    兼容 Claude 技能包（SKILL.md）的 YAML frontmatter 子集：
    - 引号值剥离（_unquote）：description: "..." → description
    - 多行值：块标量 |（保留换行）/ >（折叠空格），以及隐式续行（缩进纯文本归属上一键）
    """
    meta: dict = {}
    body = text.strip()
    if text.lstrip().startswith("---"):
        lines = text.splitlines()
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        if end is not None:
            last_key: str | None = None
            block_mode: str | None = None  # "|"（保留换行）/ ">"（折叠空格）
            block_lines: list[str] = []
            for i in range(1, end):
                line = lines[i]
                stripped = line.strip()
                if block_mode is not None:
                    if stripped == "" or line[:1] in (" ", "\t"):
                        block_lines.append(stripped)
                        continue
                    # 块标量结束：收尾写入
                    if block_mode == ">":
                        meta[last_key] = _clean("\n".join(block_lines))
                    else:
                        meta[last_key] = "\n".join(block_lines)
                    block_mode = None
                    block_lines = []
                    if stripped == "---" or not stripped:
                        continue
                if not stripped or stripped.startswith("#"):
                    continue
                if ":" in stripped:
                    key, _, value = stripped.partition(":")
                    key = key.strip()
                    value = value.strip()
                    if value in ("|", ">", "|-", ">-", "|+", ">+"):
                        # 块标量开始：等待后续缩进行
                        last_key = key
                        block_mode = "|" if value.startswith("|") else ">"
                        block_lines = []
                        continue
                    meta[key] = _unquote(value)
                    last_key = key
                elif last_key is not None and line[:1] in (" ", "\t"):
                    # 隐式续行：缩进的纯文本行归属上一个键
                    meta[last_key] = _clean(f"{meta[last_key]} {stripped}")
            if block_mode is not None:
                # 块标量一直延续到 frontmatter 结束
                if block_mode == ">":
                    meta[last_key] = _clean("\n".join(block_lines))
                else:
                    meta[last_key] = "\n".join(block_lines)
            body = "\n".join(lines[end + 1:]).strip()
    return meta, body


def _build_skill_md(name, display_name, description, category, version, body) -> str:
    """按 frontmatter + 正文 生成 skill.md 内容（开关状态不写入 skill.md，统一存 skill_config.json）。"""
    return (
        "---\n"
        f"name: {name}\n"
        f"display_name: {_clean(display_name)}\n"
        f"description: {_clean(description)}\n"
        f"category: {category}\n"
        f"version: {version}\n"
        "---\n\n"
        f"{body}\n"
    )


# ---------------------------------------------------------------- 管理器


class SkillsManager:
    """技能池管理器：内置技能只读，自定义技能持久化到 skills_pool/<name>/。"""

    def __init__(self, pool_dir: Path | None = None) -> None:
        self.pool_dir = pool_dir

    def _pool_dir(self) -> Path:
        return self.pool_dir or SKILLS_POOL_DIR

    # ---------------- 初始化 ----------------

    def init(self) -> None:
        """启动初始化：注册内置技能 + 加载自定义技能，统一应用持久化的开关状态（skill_config.json）。"""
        _registry.registry.clear()
        for cls in BUILTIN_SKILLS:
            _registry.registry.register(cls())
        if self._pool_dir().exists():
            for skill_dir in sorted(self._pool_dir().iterdir()):
                if skill_dir.is_dir():
                    try:
                        _registry.registry.register(self._load_skill_dir(skill_dir))
                    except (ValueError, OSError):
                        continue  # 跳过损坏 / 不完整目录
        # 统一应用所有技能（内置 + 自定义）的持久化开关状态
        config = _load_skill_config()
        states = config.get("skills", {})
        # 迁移早期内置技能 ID；新 ID 已配置时优先使用新值。
        for action in ("search", "analyze", "write", "review"):
            old_name = "mock_" + action
            new_name = "builtin_" + action
            if old_name in states and new_name not in states:
                states[new_name] = states[old_name]
        for name, enabled in states.items():
            skill = _registry.registry.get(name)
            if skill is not None:
                skill.enabled = bool(enabled)
        # 同步一次配置（兼容旧 skill.md 中已存在的 enabled 字段，迁移进 skill_config.json）
        _save_skill_config()

    # ---------------- 查询 ----------------

    def list_skills(self) -> list[dict]:
        return [skill.to_dict() for skill in _registry.registry.get_all()]

    def get_skill(self, name: str) -> BaseSkill | None:
        """按名称获取技能（不存在返回 None）。"""
        return _registry.registry.get(name)

    def list_enabled_schemas(self, excluded_names: set[str] | None = None) -> list[dict]:
        """已启用技能的 OpenAI function schema（供 single_agent 注入 LLM tools 参数）。

        与已有工具（内置 / MCP）重名的技能被跳过，避免 OpenAI 因重复工具名报错。
        """
        excluded = excluded_names or set()
        schemas = []
        for skill in _registry.registry.get_all():
            if not skill.enabled or skill.name in excluded:
                continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": skill.name,
                        "description": f"【技能·{skill.display_name}】{skill.description or '执行该技能的任务'}",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "交给技能执行的具体请求内容",
                                },
                            },
                            "required": ["query"],
                        },
                    },
                }
            )
        return schemas

    # ---------------- 创建 / 更新 / 删除 ----------------

    def create_skill(
        self, name, display_name, description="", category="analysis", version="1.0.0", skill_md="", enabled=True
    ) -> dict:
        name = name.strip()
        if not NAME_RE.match(name):
            raise ValueError("技能名仅支持字母、数字、下划线、中划线（且不能以 -/_ 开头）")
        if category not in VALID_CATEGORIES:
            raise ValueError(f"分类必须是 {'、'.join(VALID_CATEGORIES)} 之一")
        if _registry.registry.get(name) is not None:
            raise ValueError(f"技能已存在：{name}")

        body = (skill_md or "").strip() or DEFAULT_SKILL_BODY
        skill_dir = self._pool_dir() / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "skill.md").write_text(
            _build_skill_md(name, display_name or name, description or "", category, version or "1.0.0", body),
            encoding="utf-8",
        )
        skill = MarkdownSkill(
            {"name": name, "display_name": display_name or name, "description": description,
             "category": category, "version": version or "1.0.0", "enabled": enabled},
            body,
        )
        _registry.registry.register(skill)
        _save_skill_config()  # 开关状态统一写入 skill_config.json
        return skill.to_dict()

    def update_skill(self, name, display_name=None, description=None, category=None, version=None, skill_md=None, enabled=None) -> dict:
        skill = _registry.registry.get(name)
        if skill is None:
            raise KeyError(f"技能不存在：{name}")
        if skill.builtin:
            raise ValueError(f"内置技能不可修改：{name}")
        if category is not None and category not in VALID_CATEGORIES:
            raise ValueError(f"分类必须是 {'、'.join(VALID_CATEGORIES)} 之一")

        skill_dir = self._pool_dir() / name
        md_file = skill_dir / "skill.md"
        if md_file.exists():
            meta, body = _parse_skill_md(md_file.read_text(encoding="utf-8"))
        else:
            meta, body = {"name": name}, ""
        meta["name"] = name
        if display_name is not None:
            meta["display_name"] = display_name
        if description is not None:
            meta["description"] = description
        if category is not None:
            meta["category"] = category
        if version is not None:
            meta["version"] = version
        if skill_md is not None:
            body = skill_md.strip() or body

        skill_dir.mkdir(parents=True, exist_ok=True)
        md_file.write_text(
            _build_skill_md(
                name, meta.get("display_name") or name, meta.get("description") or "",
                meta.get("category") or "analysis", meta.get("version") or "1.0.0", body,
            ),
            encoding="utf-8",
        )
        skill.display_name = meta.get("display_name") or name
        skill.description = meta.get("description") or ""
        skill.category = meta.get("category") or "analysis"
        skill.version = meta.get("version") or "1.0.0"
        skill.skill_md = body
        if enabled is not None:
            skill.enabled = bool(enabled)
            _save_skill_config()  # 开关状态统一写入 skill_config.json
        return skill.to_dict()

    def delete_skill(self, name: str) -> None:
        skill = _registry.registry.get(name)
        if skill is None:
            raise KeyError(f"技能不存在：{name}")
        if skill.builtin:
            raise ValueError(f"内置技能不可删除：{name}")
        _registry.registry.unregister(name)
        shutil.rmtree(self._pool_dir() / name, ignore_errors=True)
        _save_skill_config()  # 同步移除已删除技能的开关记录

    def set_enabled(self, name: str, enabled: bool) -> dict:
        """开关技能（内置 / 自定义均可），统一持久化到 skill_config.json。"""
        skill = _registry.registry.set_enabled(name, enabled)
        _save_skill_config()
        return skill.to_dict()

    # ---------------- zip 导入 ----------------

    def import_skill(self, data: bytes) -> dict:
        """导入 .zip 技能包：校验 → 解压 → 解析 skill.md → 注册。"""
        infos = validate_archive(data, MAX_ZIP_SIZE)
        zf = zipfile.ZipFile(io.BytesIO(data))
        entries = [info.filename for info in infos]
        root = self._find_skill_root(entries)
        if root is None:
            raise ValueError("zip 包内未找到 skill.md（需位于根目录或唯一的顶层目录）")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for info in infos:
                name = info.filename
                if info.is_dir():
                    continue
                if root and not name.startswith(root):
                    raise ValueError("技能包包含顶层目录之外的文件")
                rel = name[len(root):] if root else name
                dest = tmp_path / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src:
                    content = src.read(MAX_ZIP_SIZE + 1)
                if len(content) > MAX_ZIP_SIZE:
                    raise ValueError("技能包解压后体积过大")
                dest.write_bytes(content)

            # 兼容 Claude 技能包：SKILL.md 统一归一化为 skill.md 再持久化
            # （Windows 文件系统不区分大小写，不能依赖 exists() 判断，需按名称匹配）
            for p in tmp_path.iterdir():
                if p.is_file() and p.name.lower() == "skill.md" and p.name != "skill.md":
                    p.rename(tmp_path / "skill.md")
                    break

            md_text = _read_text_fallback(tmp_path / "skill.md")
            meta, body = _parse_skill_md(md_text)
            skill_name = str(meta.get("name") or "").strip()
            if not NAME_RE.match(skill_name):
                raise ValueError("skill.md 中的 name 不合法")
            if _registry.registry.get(skill_name) is not None:
                raise ValueError(f"技能已存在：{skill_name}")

            target = self._pool_dir() / skill_name
            if target.exists():
                raise ValueError(f"技能目录已存在：{skill_name}")
            target.mkdir(parents=True, exist_ok=False)
            try:
                for item in tmp_path.iterdir():
                    if item.is_dir():
                        shutil.copytree(item, target / item.name)
                    else:
                        shutil.copy2(item, target / item.name)
                skill = self._load_skill_dir(target)
                _registry.registry.register(skill)
                _save_skill_config()
                return skill.to_dict()
            except Exception:
                _registry.registry.unregister(skill_name)
                shutil.rmtree(target)
                raise

    # ---------------- 执行 ----------------

    async def run_skill(self, name: str, query: str, context: dict | None = None) -> SkillOutput:
        skill = _registry.registry.get(name)
        if skill is None:
            raise KeyError(f"技能不存在：{name}")
        if not skill.enabled:
            raise ValueError(f"技能已禁用：{name}")
        return await skill.execute(SkillInput(query=query, context=context or {}))

    # ---------------- 内部工具 ----------------

    @staticmethod
    def _find_skill_root(entries: list[str]) -> str | None:
        """定位 skill.md（兼容 Claude 技能包的 SKILL.md）：根目录或唯一顶层目录下。"""
        if any(e.lower() == "skill.md" for e in entries):
            return ""
        roots = {e.split("/")[0] for e in entries if "/" in e}
        for root in sorted(roots):
            if any(e.lower() == f"{root}/skill.md" for e in entries):
                return root + "/"
        return None

    def _load_skill_dir(self, skill_dir: Path) -> BaseSkill:
        """从目录加载技能：优先 skill.py，缺失则生成 MarkdownSkill（兼容 SKILL.md）。"""
        md_file = skill_dir / "skill.md"
        if not md_file.exists() and (skill_dir / "SKILL.md").exists():
            md_file = skill_dir / "SKILL.md"
        if not md_file.exists():
            raise ValueError(f"技能目录缺少 skill.md：{skill_dir.name}")
        meta, body = _parse_skill_md(md_file.read_text(encoding="utf-8"))
        skill = self._load_skill_py(skill_dir, meta, body)
        if skill is None:
            # 传入 skill_dir：MarkdownSkill 会顺带扫描 references/ 与 scripts/ 资源
            skill = MarkdownSkill(meta, body, skill_dir=skill_dir)
        return skill

    def _load_skill_py(self, skill_dir: Path, meta: dict, body: str) -> BaseSkill | None:
        """动态加载 skill.py（可选）：必须定义 BaseSkill 子类，以 skill.md 元数据为准。"""
        py_file = skill_dir / "skill.py"
        if not py_file.exists():
            return None
        module_name = f"_skill_{uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, py_file)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise ValueError(f"skill.py 加载失败：{exc}") from exc
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, BaseSkill) and value is not BaseSkill:
                instance = value()
                instance.name = str(meta.get("name") or instance.name)
                instance.display_name = str(meta.get("display_name") or instance.display_name)
                instance.description = str(meta.get("description") or instance.description)
                instance.category = str(meta.get("category") or instance.category)
                instance.version = str(meta.get("version") or instance.version)
                instance.builtin = False  # 导入的技能一律按自定义管理
                instance.enabled = str(meta.get("enabled", "true")).strip().lower() != "false"
                instance.skill_md = body
                return instance
        raise ValueError("skill.py 未定义 BaseSkill 子类")


manager = SkillsManager()
