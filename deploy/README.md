# SmartBrief 容器化部署（deploy/）

将 SmartBrief 全栈（前端 + 后端多智能体引擎 + MCP Server）打包为两个 Docker 镜像，用 Docker Compose 一键编排，**前后端零代码改动**即可运行。

## 一、架构

```text
浏览器 ──► http://localhost:8080
              │
              ▼  nginx（frontend 容器，负责静态托管 + 反代）
   /api/* ────► proxy_pass http://backend:8002
              │
              ▼
        backend 容器（FastAPI）
           │  以子进程自动拉起 mcp_servers/web_search_server.py（MCP 工具）
           ▼
   ┌───────────────────┬──────────────────┐
   │ 数据卷 backend_data│ 环境变量（.env）  │
   │  chroma_db、uploads│ OPENAI_* /       │
   │  templates、terms  │ EMBEDDING_*      │
   └───────────────────┴──────────────────┘
```

- **一个前端镜像**：多阶段构建（node 打包 dist → nginx 运行），nginx 同时承担 `/api` 反向代理与 SPA 路由回退
- **一个后端镜像**：python slim + 后端源码 + mcp_servers（容器内保持 `backend/` 与 `mcp_servers/` 平级，MCP 子进程才能按相对路径拉起）

## 二、目录结构

```text
deploy/
├── docker-compose.yml        # 开发编排：backend + frontend（含健康检查、卷、端口）
├── docker-compose.prod.yml   # 云端编排：无 build，直接用上传的镜像
├── .env.example              # 环境变量示例（LLM / 嵌入 Key）
├── start.sh / start.bat      # 本地一键构建并启动
├── export-images.sh / .bat   # 只打包镜像（docker save → tar[.gz]）
├── deploy-to-server.sh / .bat # 一键：构建 → 打包 → 上传 → 云端 load + up -d
├── README.md                 # 本文档
├── backend/
│   ├── Dockerfile            # 后端镜像（含 seed 备份 + entrypoint）
│   └── entrypoint.sh         # 首启把 RAG 预置 seed（模板/术语）复制进空卷
└── frontend/
    ├── Dockerfile            # 前端镜像（node 构建 → nginx 运行）
    └── nginx.conf            # 静态托管 + /api 反代 + SPA 回退
```

> 另外仓库根目录有 `.dockerignore`（构建上下文是仓库根，排除 node_modules / 运行期数据 / 密钥等）。

## 三、快速开始

### 1. 前置条件

- Docker Engine ≥ 24 与 Docker Compose v2（`docker compose version` 可验证）
- 真实可用的 LLM API Key（容器内无法使用本地 `backend/.env`，一律走 `deploy/.env`）
- 两个互不相同的访问令牌：管理员令牌必填，只读令牌可选但建议配置

### 2. 配置环境变量

```bash
cd deploy
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY（必填）与 EMBEDDING_API_KEY（RAG 用，可选）
```

生成并填写访问令牌（至少 32 位随机串，两个值不能相同）：

```bash
python -c "import secrets;print('SMARTBRIEF_ADMIN_TOKEN='+secrets.token_hex(24))"
python -c "import secrets;print('SMARTBRIEF_READONLY_TOKEN='+secrets.token_hex(24))"
```

未配置管理员令牌时，除匿名探针 `/healthz` 外所有接口返回 503。浏览器打开前端后输入管理员或只读令牌登录；令牌只保存在页面内存中，刷新后需重新输入。管理员可读写，只读令牌仅能查询。

### 3. 一键启动

```bash
# Linux / Mac
./start.sh

# Windows（PowerShell / cmd）
start.bat
```

脚本会：检查 `.env` → `docker compose build`（首次构建需下载基础镜像与 pip 依赖，耗时较长）→ `docker compose up -d` → 打印状态。

### 4. 访问

| 入口 | 地址 | 说明 |
|------|------|------|
| 前端 | <http://localhost:8080> | 全部页面（任务提交/列表/报告中心/评测中心/知识库/对话/工具/技能），需输入访问令牌 |
| 匿名探针 | <http://localhost:8002/healthz> | 不需要令牌，返回 `{"status":"ok"}` 即正常（容器健康检查使用） |
| 后端健康检查 | <http://localhost:8002/api/v1/health> | 需要 Bearer 令牌；不带令牌返回 401 |
| 后端 API 直连 | <http://localhost:8002/api/v1> | 可跳过 nginx 直接调试，同样需要令牌 |

## 四、常用运维命令（在 deploy/ 目录）

```bash
docker compose ps                    # 查看状态（backend 应显示 healthy）
docker compose logs -f backend       # 跟踪后端日志（含评测进度、MCP 拉起）
docker compose logs -f frontend      # 跟踪 nginx / 代理日志
docker compose restart               # 重启（保留数据）
docker compose down                  # 停止并删除容器（保留卷数据）
docker compose up -d --build         # 代码更新后重建镜像并滚动升级
docker compose down -v               # ⚠️ 同时删除卷（backend_data 数据将清空，谨慎）
```

## 五、数据持久化说明（重要）

| 数据 | 存储位置 | 持久化行为 |
|------|----------|-----------|
| ChromaDB 向量库（RAG 知识库 + 长期记忆）、上传文档、模板/术语 seed | 卷 `smartbrief_backend_data`（挂载 `/app/backend/data`） | **长期保留**，`down`/`up` 不丢 |
| 任务记录（`data.db`）、图快照（`checkpoints.db`）、工具/MCP/技能开关配置（`*_config.json`） | 容器可写层（`/app/backend`） | `docker compose restart` 保留；**删除并重建容器（`down`/`up` 或 `--build`）会重置** |

如需把第二类数据也持久化，可取消 docker-compose.yml 中的文件级 bind 挂载并先创建宿主文件：

```yaml
volumes:
  - backend_data:/app/backend/data
  - ./volumes/data.db:/app/backend/data.db
  - ./volumes/checkpoints.db:/app/backend/checkpoints.db
  - ./volumes/skill_config.json:/app/backend/skill_config.json
```

> 首次启动时 entrypoint 会把镜像内置的 5 份模板与 30 条术语复制进空卷（`cp -n` 幂等、不覆盖），随后后端 lifespan 执行 RAG seed，与本地开发行为一致。

## 六、云服务器部署（本地打包 → 上传）

无需在云服务器上构建（避免云端拉依赖慢、暴露源码/密钥），本地构建镜像后 `docker save` 打包上传、云端 `docker load` 直接运行。

### 思路

```text
本地（有源码 + Docker）
  1. docker compose build           构建两个镜像
  2. docker save … | gzip           打包为单一 tar.gz（含 backend + frontend）
  3. scp / ssh                      上传镜像包 + docker-compose.prod.yml
云服务器（仅需 Docker）
  4. docker load -i                 载入镜像（免构建）
  5. docker compose -f docker-compose.prod.yml up -d   启动
```

`deploy/docker-compose.prod.yml` 是**无 build 的独立编排**（直接 `image:` 引用上传的镜像），与开发编排等价（健康检查/数据卷/端口一致）。

### 一键脚本

```bash
# Linux / Mac：构建 → 打包 → 上传 → 云端加载并启动
./deploy-to-server.sh root@1.2.3.4 /opt/smartbrief

# Windows（PowerShell / cmd）
deploy-to-server.bat root@1.2.3.4 /opt/smartbrief
```

脚本行为：

1. 本地 `docker compose build` 后 `docker save` 打包
2. 经 SSH 上传：镜像包 + `docker-compose.prod.yml` + `.env.example`（本地 `deploy/.env` 若存在也会上传，**含密钥，请确认服务器可信**）
3. 云端 `docker load` → 若 `deploy/.env` 缺失则从示例生成并提示编辑（首次运行后需在服务器上改 `.env` 再重启）→ `docker compose -f docker-compose.prod.yml up -d`

只打包不部署时用 `export-images.sh` / `export-images.bat`（产物 `smartbrief-images.tar.gz` 或 `.tar`），目标机手动：`docker load -i smartbrief-images.tar.gz && docker compose -f docker-compose.prod.yml up -d`。

### 云服务器准备

- 安装 Docker Engine + Compose v2（`curl -fsSL https://get.docker.com | sh`），当前用户加入 docker 组或使用 root
- 安全组放行：`8080`（前端，可改映射为 80）、`8002`（API 直连，可选）
- 首次部署后编辑服务器的 `deploy/.env` 填入真实 `OPENAI_API_KEY` 等，再 `docker compose -f docker-compose.prod.yml up -d`（或重启 backend）
- 数据卷 `smartbrief_backend_data` 由云端首次 `up` 自动创建；升级版本重新执行部署脚本即可，数据不丢

## 七、环境变量一览

| 变量 | 必填 | 说明 |
|------|------|------|
| `OPENAI_BASE_URL` | 是 | OpenAI 兼容接口地址（DeepSeek / Kimi / Ollama 均可） |
| `OPENAI_API_KEY` | 是 | 接口密钥 |
| `OPENAI_MODEL` | 否 | 模型名，默认 `gpt-4o-mini` |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` | 否 | RAG 嵌入（SiliconFlow bge-m3）；未配置则知识库为空、任务照常执行 |
| `MEMORY_ENABLED` | 否 | 长期记忆开关（默认 true） |
| `TZ` | 否 | 容器时区（默认 UTC；建议 `Asia/Shanghai`，影响日志与 SQLite 时间字段） |

## 八、常见问题

1. **任务失败 / 对话报错 "未配置 OPENAI_API_KEY"**：确认 `deploy/.env` 已填写且 `docker compose up -d` 后执行 `docker compose restart backend`（env_file 变更需重启容器生效）。
2. **前端页面 502 / API 无响应**：`docker compose ps` 看 backend 是否 `healthy`；首次启动需 30-60s（RAG seed + MCP Server 拉起）；可用 `docker compose logs backend` 查看报错。
3. **SPA 路由直接刷新 404**：应回退 index.html；若自定义了 nginx 配置，检查 `try_files $uri $uri/ /index.html;` 是否保留。
4. **端口被占用**：修改 docker-compose.yml 的 `"8080:80"` / `"8002:8002"` 左侧端口即可。
5. **容器内 MCP web_search 不可用**：后端镜像必须同时包含 `mcp_servers/`（Dockerfile 已 COPY）；确认容器可访问外网（搜索工具需出网）。
6. **时间显示为 UTC**：在 `.env` 加 `TZ=Asia/Shanghai` 后重启后端容器。
7. **与本地开发的区别**：容器版为生产形态（无热重载、统一 nginx 网关、数据在卷）；本地开发使用仓库根目录的 `start.bat`（uvicorn + vite）。