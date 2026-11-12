# SmartBrief · 多智能体报告生成平台

SmartBrief 将研究主题转化为结构化报告，覆盖研究规划、网络资料检索、内容撰写、审核返工与结果导出。用户可以查看执行计划、工具调用记录、审核意见和历史报告。

当前版本：**0.2.0**，已完成本地范围最终验收；Docker 与在线效果未验收。

## 核心能力

- **研究工作流**：Planner 生成带依赖的计划，Scheduler 按依赖顺序串行执行研究与写作步骤，Reviewer 审核草稿，Rewriter 按意见返工。
- **知识库与记忆**：模板辅助规划、术语辅助写作；支持选择上传的 txt、md、docx、pdf、xlsx 文档参与研究并查看检索解释；仅质量通过且来源合格的报告进入可信记忆，支持失效、删除与逻辑开关。
- **访问边界**：管理员/只读令牌认证，文件与ZIP前置安全校验；自定义Python和脚本默认禁用，仅由所有者后端配置开启。
- **任务管理**：单 worker 受控排队，支持取消、幂等动作、分页与筛选；服务中断后任务暂停，恢复时明确展示断点续跑或从头重试。模型用量与任务预算独立持久化，未知值保留未知。
- **报告中心**：主题搜索、时间排序、Markdown 展示及 Word / PDF 导出。
- **工具与技能**：内置时间、计算工具；MCP 支持 stdio、Streamable HTTP 与 SSE；技能支持启停、自定义指令与 ZIP 导入。
- **评测中心**：默认执行 protocol-1 固定 20 例离线合成协议，展示进度、执行/质量指标、引用结构与单变量比较；在线研究效果及人工事实抽查另行验收。
- **离线演示与性能**：显式导入正常报告、文档溯源、服务中断三类样例；样例和导出含演示标识。页面路由、图表与 PDF 导出按需加载，性能口径见 [性能记录](docs/performance.md)。

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

构建包含 TypeScript 类型检查。

## 当前限制

- 调度器按依赖串行执行，未实现步骤并行调度。
- 只读令牌是本实例全部查询资源的读取凭证，包含接口返回的配置、文档内容、日志与用量；当前没有逐报告分享或多租户隔离。
- 搜索依赖 Bing HTML 页面解析；网络失败返回错误，无结果返回空列表，不生成虚构来源。
- 执行完成与质量通过分别展示；审核异常/解析失败为 unavailable，返工达到上限保留 rejected 与强制输出标识。引用校验只证明结构可追溯，不证明事实真实或语义支持。
- 默认 20 例离线合成评测只验证软件协议；v1 在线冒烟接口保留兼容，仍可能产生真实费用。关键词覆盖率不是事实准确率，在线效果和人工事实抽查尚未执行。
- 任务使用进程内后台执行；评测并发保护限于当前进程，尚未采用独立任务队列。
- 检查点存储不可用时降级为进程内存，无法保证跨进程恢复。
- Docker 默认数据卷主要覆盖 `backend/data/`；业务库、检查点和本地配置的持久化需按部署说明额外设置。
- 本地数据支持绝对路径 `DATA_DIR`（默认保留backend旧布局），统一业务库、检查点、向量、上传、配置和自定义技能。停写备份与全新目录恢复见[备份恢复手册](docs/runbooks/data-backup-restore.md)；Docker相关工作由用户延后，未将本地验收当作容器成绩。

代码来源与适配记录见 [来源说明](docs/ATTRIBUTION.md)。

## 主备模型、Agent级恢复与缓存用量

- 研究子任务和每个章节分别保存图检查点；中断后保留已完成单元，只重做未完成Agent。审核、补研究、返工也按各自节点恢复。不支持Agent内部轮次或单次生成中途续写；检查点不可用时明确从头重试。
- 主备默认关闭。配置`MODEL_FAILOVER_ENABLED=true`及`BACKUP_OPENAI_BASE_URL`、`BACKUP_OPENAI_API_KEY`、`BACKUP_OPENAI_MODEL`后启用；备用需支持相同的消息/工具接口与足够上下文，真实兼容性另行验证。
- 主模型发生可接管故障后，当前任务绑定备用，恢复或restart也不回切。主模型的恢复只由冷却后的新任务实际请求试探；无额外收费健康检查。主备均不可用则暂停并保留进度；预算、取消及持久化错误不触发接管。
- 用量面板可查看主备、供应商、切换原因、Agent恢复位置及缓存读取/写入Token。缓存读取包含于输入总量，不重复扣预算；供应商字段缺失或无必要单价时保留未知。历史费用不根据新价格重算。
- `MODEL_PRICES_JSON`优先使用`provider:model`键，也兼容模型键；普通输入/输出单价以每百万Token配置，缓存命中需`cache_read_per_million`。写入另计费时配置`cache_write_per_million`及`cache_write_mode`：`included`替换对应输入价格，`additional`叠加写入费，按供应商实际规则选择。金额是估算，不是供应商账单。
- 数据版本升级为4（新增任务模型绑定表）。开发验收不修改真实库；首次运行新代码前按[回滚与备份说明](docs/runbooks/git-and-data-rollback.md)停写备份业务库与检查点。配置示例见`backend/.env.example`，密钥不要提交Git。


### 结构化报告 v3（显式选择）

默认新任务使用v2；创建API显式传入`report_pipeline_version: 3`可使用严格文档模型、程序引用渲染与按块返工。文档在schema5的可空`report_document`列保存，v1/v2旧任务继续按原版本读取/恢复。使用新代码前按[数据回滚手册](docs/runbooks/git-and-data-rollback.md)完成停写备份；本轮只验收隔离迁移，没有操作真实运行库。字段与模型职责见[v3契约对照](docs/construction/v3-model-contracts.md)。离线通过不代表主模型在线效果已验收。
