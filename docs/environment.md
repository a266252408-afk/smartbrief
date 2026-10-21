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

守卫适用于本项目 Python 测试与 MCP 测试进程，不是通用操作系统网络沙箱。测试夹具不能作为在线模型效果成绩。前端测试入口见下文。


## 前端断言与真实浏览器

在 frontend/ 先按锁运行 `npm ci --cache .npm-cache`，然后执行：

```powershell
npm run test:unit -- --run
npm run build
$env:PLAYWRIGHT_BROWSERS_PATH=(Join-Path (Get-Location) '.browser-cache')
npx playwright install chromium
npm run test:e2e
```

Playwright 配置默认使用项目内 `.browser-cache`；浏览器安装和运行路径须一致。Linux/macOS 对应设置 `PLAYWRIGHT_BROWSERS_PATH="$PWD/.browser-cache"` 后安装 Chromium，目标系统依赖需在该平台独立验证。本轮验证的是 Windows Chromium，不宣称其他平台已验收。

单测使用真实 Home 组件和 Element Plus，只替换 API 与路由边界；E2E 自动启动本地 Vite（127.0.0.1:5175，端口被占用会失败），运行真实 Chromium、真实页面/路由与明确离线 HTTP 夹具，拒绝未声明的 API 与外部请求，不启动付费后台工作流。后端真实 API/图/仓储集成独立执行，不用页面夹具代替。任务 T11/T13 再验证完整演示和最终交付。

`test-results/`、`playwright-report/` 和浏览器缓存为忽略的本地产物；失败保留 trace。`npm run test:e2e` 不复用已有 Vite 服务，不需手动启动用户浏览器。测试缺包、缺浏览器、没有断言或依赖失败都不算通过。

## 访问认证配置

后端环境设置不同的 `SMARTBRIEF_ADMIN_TOKEN`、可选 `SMARTBRIEF_READONLY_TOKEN`（无强制位数要求、非空且无空白），直接使用配置值精确匹配，勿提交真实值。`SMARTBRIEF_CORS_ORIGINS` 为逗号分隔的完整HTTP(S)来源，不允许通配符；修改后重启服务。默认允许本地5174前端。

只有 GET/HEAD `/healthz` 匿名。其余入口（含文档/OpenAPI）需 `Authorization: Bearer <token>`；v2 `/api/v2/auth/session` 返回角色包，只读允许查询，不允许生成、修改、删除或执行。缺配置503、缺失/错误令牌401、角色不足403。浏览器令牌仅内存保存，401清空重新认证、403保留只读会话并提示。单所有者角色不代表多租户隔离。

## 上传边界

文档限10MiB，允许txt/md/docx/pdf/xlsx，拒绝跨目录名、驱动器、Windows保留名和控制字符；落盘使用服务端UUID，展示名/source兼容保留。新上传的document_id同步到chunk metadata，不同上传的相同文本不混用来源，旧索引不原地重写。技能ZIP压缩/解压均限2MiB、最多256条、压缩比上限1000，拒绝穿越、链接、重复路径和加密条目。代码执行默认关闭，见下文。

## 自定义代码信任开关

`SMARTBRIEF_ALLOW_SKILL_PYTHON=false` 与 `SMARTBRIEF_ALLOW_SKILL_SCRIPTS=false` 为默认。管理员仅在信任代码时于后端环境显式设为1或true，不能通过技能metadata、提示或API请求context启用。Python禁用时不导入skill.py，保留Markdown能力；脚本禁用时不发现或注入工具，直接调用也拒绝。已加载Python和脚本在运行前重新检查开关。

启用代表所有者信任本机代码，不是Python沙箱；仍限制脚本目录与参数传递、禁shell并保留超时，但受信任Python可拥有后端进程权限。前端没有给模型或只读角色开放启用此开关的接口。

## T03 数据库升级前置

T03新增任务双状态列并事务回填，启动时init_db会执行升级（user_version=1）。真实实例首次运行新版本前，必须按[回滚手册](runbooks/git-and-data-rollback.md)停写，仓库外备份业务库/检查点/向量库/上传及非密钥配置，并在隔离副本校验完整性和恢复。测试仅使用临时旧库，不表示真实实例已经备份或升级。旧passed=True不足以证明质量通过，迁移后为unknown；原记录保留。

## T04 来源持久化升级

T04新增tasks.evidence JSON列（历史默认空列表，不反推来源），纳入同一补列事务；后续引用列随T04-02独立升级。真实启动前沿用停写、仓库外一致性备份与隔离恢复校验要求。本轮只测试临时旧库，未备份或升级真实实例。

## T10 运行数据根与版本

`DATA_DIR`在后端进程启动时读取。不设置时兼容原backend目录；设置时必须为非空绝对路径，例如`D:/SmartBriefData`。运行根下保存`data.db`、`checkpoints.db`、`data/chroma_db/`、`data/uploads/`、三个运行配置JSON与`skills_pool/`。代码的模板/术语仍从`backend/data/templates`及`backend/data/terminologies.txt`只读加载；环境密钥继续由环境变量或backend/.env提供，不复制进数据根。切换目录不会自动迁移旧数据，必须停服务后按备份恢复手册恢复到新目录再设置DATA_DIR。

T10数据版本为2，`schema_migrations`记录应用版本和UTC时间；旧库建表、补列、质量回填与版本记录同一事务，二次启动不重复记录，未来版本拒绝启动。真实数据升级前先停写并验证备份；旧代码切换不能替代配套数据恢复。2026-10-21用户要求Docker决策延后，容器重建/恢复成绩保持待验。
