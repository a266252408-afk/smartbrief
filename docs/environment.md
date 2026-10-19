# 可复现本地环境

T01-01 验证平台为 Windows x64，Python 3.11.9、Node.js 24.18.0、npm 11.17.0。根目录 `.python-version` / `.node-version` 记录版本，安装器需自行读取。其他平台和容器需在 T10/T13 独立验证，不把本机结果当容器成绩。

## 安装与启动

在根目录执行：

```powershell
python --version
node --version
npm --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r backend/requirements-dev.lock
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\Activate.ps1
cd frontend
npm ci --cache .npm-cache
npm run build
cd ..
```

每条命令检查退出码，失败即停止。`.venv` 不继承全局包。只运行服务时安装 `backend/requirements.lock`，无需 pytest、覆盖率、Ruff 与锁维护工具。Linux/macOS 的 Python 路径为 `.venv/bin/python`；锁文件的跨平台可安装性仍需在目标平台实测。

配置 `backend/.env` 后使用 `start.bat` 或 `bash start.sh`。启动脚本只使用项目 `.venv`，缺失时明确失败。手动运行可在 backend 目录调用 `..\.venv\Scripts\python.exe -m uvicorn main:app --port 8002`。模型与嵌入调用须另行配置，安装和离线测试不需要付费 Key。

## 依赖维护

`requirements.txt` 是生产直接依赖输入，`requirements.lock` 是包含传递依赖及哈希的完整锁；`requirements-dev.in` 是开发输入，`requirements-dev.lock` 是包含生产依赖的完整开发锁。前端提交 `package-lock.json`，验收用 `npm ci`，不使用隐式更新锁的 `npm install`。

在独立维护环境安装 `pip-tools==7.5.2`，从根目录更新：

```powershell
python -m piptools compile --cache-dir .validation/pip-tools --resolver=backtracking --generate-hashes --allow-unsafe --strip-extras --output-file backend/requirements.lock backend/requirements.txt
python -m piptools compile --cache-dir .validation/pip-tools --resolver=backtracking --generate-hashes --allow-unsafe --strip-extras --constraint backend/requirements.lock --output-file backend/requirements-dev.lock backend/requirements-dev.in
```

更新锁后必须从空环境重新安装、`pip check`、运行后端全量测试和前端构建。不要把混入其他项目依赖的全局 `pip freeze` 写为项目锁。

## 测试阶段

T01-01 提供隔离源码副本运行入口，复制范围限 Git 跟踪的源码，排除真实数据库、运行配置、密钥、上传目录和向量库。T01-02 在 pytest 收集前禁用 dotenv 与外部 DNS/连接，允许本地回环和 IPC；默认数据库、检查点、上传、向量库与运行配置均重定向到临时目录。MCP 测试服务复用生产协议，只替换外部搜索边界，返回明确 fixture 来源。共享 ScriptedLLM / FakeEmbeddings 可供后续模块使用。

标准后端命令（backend/）：`python -m pytest tests/ -v`；静态检查：`python -m ruff check .`。额外从根目录运行 `python -m ruff check backend mcp_servers scripts` 覆盖独立服务与脚本。Windows 执行环境需允许本地命名管道，沙箱拒绝时必须记录失败并在允许本地 IPC 的环境重跑，不能跳过。

守卫适用于本项目 Python 测试与 MCP 测试进程，不是通用操作系统网络沙箱。测试夹具不能作为在线模型效果成绩。T01-03 的前端断言与浏览器入口仍待施工。
