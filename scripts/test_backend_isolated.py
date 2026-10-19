"""在仅复制源码的临时目录运行后端回归，避免触及真实数据和密钥。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    scratch = ROOT / ".validation"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="backend-", dir=scratch) as directory:
        target = Path(directory).resolve()
        if not target.is_relative_to(scratch.resolve()):
            raise ValueError("测试副本必须位于项目验证目录")
        names = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
        ).decode("utf-8").split("\0")
        protected = {"tool_config.json", "skill_config.json", "mcp_config.json"}
        for name in names:
            source = ROOT / name
            if not name.startswith(("backend/", "mcp_servers/")) or not source.is_file():
                continue
            if source.name.startswith(".env") or source.name in protected:
                continue
            if source.suffix in {".db", ".pyc"} or name.startswith(
                ("backend/data/uploads/", "backend/data/chroma_db/")
            ):
                continue
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        env = os.environ.copy()
        for name in list(env):
            if name.startswith(("OPENAI_", "EMBEDDING_")):
                del env[name]
        env.update({"PYTHONIOENCODING": "utf-8", "SMARTBRIEF_RAG_SEED": "0"})
        return subprocess.call(
            [sys.executable, "-m", "pytest", "tests/", "-v", *sys.argv[1:]],
            cwd=target / "backend", env=env,
        )


if __name__ == "__main__":
    raise SystemExit(main())
