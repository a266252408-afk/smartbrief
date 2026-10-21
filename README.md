# SmartBrief · 多智能体报告生成平台

SmartBrief 将研究主题转化为结构化报告，覆盖研究规划、网络资料检索、内容撰写、审核返工与结果导出。用户可以查看执行计划、工具调用记录、审核意见和历史报告。

当前版本：**0.1.0**。

## 核心能力

- **研究工作流**：Planner 生成带依赖的计划，Scheduler 按依赖顺序串行执行研究与写作步骤，Reviewer 审核草稿，Rewriter 按意见返工。
- **知识库与记忆**：模板辅助规划、术语辅助写作；支持上传 txt、md、docx、pdf、xlsx 文档；历史报告可进入长期记忆，支持逻辑开关。
- **访问边界**：管理员/只读令牌认证，文件与ZIP前置安全校验；自定义Python和脚本默认禁用，仅由所有者后端配置开启。
- **任务管理**：任务与执行日志持久化，支持分页、筛选、删除；服务中断的任务重启后标记为暂停，可从检查点恢复。
- **报告中心**：主题搜索、时间排序、Markdown 展示及 Word / PDF 导出。
- **工具与技能**：内置时间、计算工具；MCP 支持 stdio、Streamable HTTP 与 SSE；技能支持启停、自定义指令与 ZIP 导入。
- **评测中心**：后台执行用例，展示进度、指标、雷达图与历史报告。默认使用一个冒烟用例验证链路。

## 架构与技术栈

```text
Vue 3 / TypeScript / Element Plus
              │ HTTP / 状态轮询
              ▼
          FastAPI API
              │
              ▼
Planner → Scheduler → Reviewer → 完成
              │          │ 不通过
     Researcher / Writer ◄┘ Rewriter → Reviewer
              │
   ┌──────────┼───────────┐
   ▼          ▼           ▼
MCP 搜索   RAG / 长期记忆  SQLite 业务库与检查点
```

后端使用 Python 3.11、FastAPI、OpenAI 兼容接口 SDK、LangGraph、ChromaDB 与 SQLite；前端使用 Vue 3、Vite、TypeScript、Element Plus、ECharts、marked 与 DOMPurify。

## 目录

```text
backend/
  agents/         规划、调度、研究、写作、审核与返工
  graph/          LangGraph 状态与工作流
  api/            HTTP 接口
  db/             业务数据与评测记录
  rag/            文档解析、分块、嵌入与向量检索
  memory/         检查点与历史报告记忆
  skills/         内置技能与技能管理
  evaluation/     冒烟评测与指标计算
  tests/          自动化测试
frontend/src/     页面、路由与 API 封装
mcp_servers/      独立搜索工具服务
deploy/           Docker Compose 与服务器部署脚本
docs/             架构、评测及来源说明
```

## 本地运行

已验证安装环境为 Python 3.11.9、Node.js 24.18.0、npm 11.17.0。详细锁定与重建方法见 [环境说明](docs/environment.md)。后端使用 8002 端口，前端使用 5174 端口。

1. 安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r backend/requirements-dev.lock
.\.venv\Scripts\Activate.ps1
cd frontend
npm ci
cd ..
```

2. 将 `backend/.env.example` 复制为 `backend/.env`，填写接口配置与两个不同的至少32字符随机访问令牌。只有 `/healthz` 为匿名最小探针，管理员令牌未配置时其余入口拒绝访问；浏览器进入时输入管理员或只读令牌，凭证仅保存于页面内存。

接口配置：

```dotenv
OPENAI_BASE_URL=https://your-provider.example/v1
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-model
EMBEDDING_BASE_URL=https://your-embedding-provider.example/v1
EMBEDDING_API_KEY=your-embedding-key
EMBEDDING_MODEL=your-embedding-model
MEMORY_ENABLED=true
```

`OPENAI_*` 用于模型调用；`EMBEDDING_*` 用于知识库与长期记忆的向量化。模型与嵌入接口可分别配置。未配置或不可用时，相关检索会降级，不能据此判断完整知识库链路已验证。

3. 运行根目录 `start.bat`（Windows）或 `bash start.sh`（Linux / macOS），也可以在两个终端分别启动：

```powershell
cd backend
python -m uvicorn main:app --host 0.0.0.0 --port 8002
```

```powershell
cd frontend
npm run dev
```

前端地址：`http://localhost:5174`；API 文档：`http://localhost:8002/docs`；匿名探针：`http://localhost:8002/healthz`；完整健康检查 `/api/v1/health` 与 API 文档均需 Bearer 令牌。

容器化与服务器部署见 [部署说明](deploy/README.md)。

## 使用流程

1. 配置模型与嵌入服务，在知识库查看模板、术语，或上传参考文档。
2. 在任务提交页输入研究主题，查看执行计划、检索日志与审核结果。
3. 在报告中心搜索历史报告，查看内容并导出 Word / PDF。
4. 开启长期记忆后，成功报告会被保存，后续相关研究可检索复用。
5. 在评测中心运行冒烟评测，检查工作流与指标采集是否正常。

详细设计见 [架构说明](docs/architecture.md)，指标口径与候选用例见 [评测说明](docs/evaluation.md)。

## 验证

在 `backend/` 执行：

```powershell
python -m pytest tests/ -v
```

测试依赖已包含在开发锁文件中。标准测试使用确定性外部服务夹具，阻止外部网络并隔离运行数据；部分测试会启动真实本地 MCP 子进程。在线模型效果验证另需明确授权，不能用离线夹具成绩替代。

在 `frontend/` 执行：

```powershell
npm run test:unit -- --run
npm run build
npm run test:e2e
```

首次浏览器测试前按 [环境说明](docs/environment.md) 安装匹配的 Chromium。

构建包含 TypeScript 类型检查。开发约定见 [CLAUDE.md](CLAUDE.md)。

## 当前限制

- 调度器按依赖串行执行，未实现步骤并行调度。
- 搜索依赖 Bing HTML 页面解析；网络失败返回错误，无结果返回空列表，不生成虚构来源。
- 审核检查数据点、来源标注与篇幅，不验证引用真实性；审核异常或解析失败可能放行，返工达到上限会强制输出并附加提示。
- 默认评测仅有一个冒烟用例；关键词覆盖率衡量文本命中，不代表事实准确率。指标目标值不是实测成绩。
- 任务使用进程内后台执行；评测并发保护限于当前进程，尚未采用独立任务队列。
- 检查点存储不可用时降级为进程内存，无法保证跨进程恢复。
- Docker 默认数据卷主要覆盖 `backend/data/`；业务库、检查点和本地配置的持久化需按部署说明额外设置。
- 本地数据支持绝对路径 `DATA_DIR`（默认保留backend旧布局），统一业务库、检查点、向量、上传、配置和自定义技能。停写备份与全新目录恢复见[备份恢复手册](docs/runbooks/data-backup-restore.md)；Docker相关工作由用户延后，未将本地验收当作容器成绩。

代码来源与适配记录见 [来源说明](docs/ATTRIBUTION.md)。

## 后续施工与跨会话接手

按 [施工清单](docs/tasks/implementation.md) 从 T01 顺序执行；先读 [项目宪法](PROJECT_CONSTITUTION.md)、[AGENTS.md](AGENTS.md) 与 [交接记录](docs/HANDOFF.md)。每个功能测试通过后提交 Git 快照，全部完成后执行 T13 全量验收。目标功能尚未实现，真实状态见 [状态文件](docs/construction/state.json) 与 [施工日志](docs/dev-log.md)。
