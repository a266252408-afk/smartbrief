# SmartBrief · 多智能体报告生成平台

SmartBrief 将研究主题转化为结构化报告，覆盖研究规划、网络资料检索、内容撰写、审核返工与结果导出。用户可以查看执行计划、工具调用记录、审核意见和历史报告。

当前版本：**0.1.0**。

## 核心能力

- **研究工作流**：Planner 生成带依赖的计划，Scheduler 按依赖顺序串行执行研究与写作步骤，Reviewer 审核草稿，Rewriter 按意见返工。
- **知识库与记忆**：模板辅助规划、术语辅助写作；支持上传 txt、md、docx、pdf、xlsx 文档；历史报告可进入长期记忆，支持逻辑开关。
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

后端使用 Python 3.10+、FastAPI、OpenAI 兼容接口 SDK、LangGraph、ChromaDB 与 SQLite；前端使用 Vue 3、Vite、TypeScript、Element Plus、ECharts、marked 与 DOMPurify。

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

准备 Python 3.10+、Node.js 与 npm。后端使用 8002 端口，前端使用 5174 端口。

1. 安装依赖：

```powershell
python -m pip install -r backend/requirements.txt
cd frontend
npm install
cd ..
```

2. 将 `backend/.env.example` 复制为 `backend/.env`，填写接口配置：

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

前端地址：`http://localhost:5174`；API 文档：`http://localhost:8002/docs`；健康检查：`http://localhost:8002/api/v1/health`。

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

测试使用 pytest，可按需安装 `pytest`、`pytest-cov`。部分测试会启动本地 MCP 子进程；完整在线链路另需可用的模型、嵌入接口与网络。

在 `frontend/` 执行：

```powershell
npm run build
```

构建包含 TypeScript 类型检查。开发约定见 [CLAUDE.md](CLAUDE.md)。

## 当前限制

- 调度器按依赖串行执行，未实现步骤并行调度。
- 搜索依赖 Bing HTML 页面解析；网络失败返回错误，无结果返回空列表，不生成虚构来源。
- 审核检查数据点、来源标注与篇幅，不验证引用真实性；审核异常或解析失败可能放行，返工达到上限会强制输出并附加提示。
- 默认评测仅有一个冒烟用例；关键词覆盖率衡量文本命中，不代表事实准确率。指标目标值不是实测成绩。
- 任务使用进程内后台执行；评测并发保护限于当前进程，尚未采用独立任务队列。
- 检查点存储不可用时降级为进程内存，无法保证跨进程恢复。
- Docker 默认数据卷主要覆盖 `backend/data/`；业务库、检查点和本地配置的持久化需按部署说明额外设置。

代码来源与适配记录见 [来源说明](docs/ATTRIBUTION.md)。
