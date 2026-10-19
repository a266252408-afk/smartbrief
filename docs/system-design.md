# 目标系统设计（施工后状态）

现状见 architecture.md，本文件为 T01～T13 的目标，不表示功能已实现。

## 模块与调用方向

```text
Vue UI → API client → FastAPI v2（鉴权、Schema、错误包）
                           ↓
                      应用服务层
          ┌───────────────┼─────────────────┐
       task_service   retrieval_service   usage_service
          ↓               ↓                  ↓
    LangGraph 工作流    文档/向量接口       模型适配器
          ↓               ↓                  ↓
      领域状态/证据 ←──── Evidence ─────── 用量记录
          ↓
    仓储（SQLite/Chroma/文件）
```

前端/API 不直连外部敏感供应商；Agent 不直写数据库。现有 workflow.py 的持久化由任务服务逐步接管，先保持兼容测试，不全仓搬迁。

## 状态与质量

execution_status：queued → running → completed | failed | cancelled；重启遗留 running → paused；paused/failed 经恢复回 queued。取消终态不被迟到回调覆盖；历史 cancelled 不自动恢复，显式重试创建新任务。

quality_status：pending → passed | rejected | unavailable | not_run；历史不可推断为 unknown。completed 仅表示流程终结且产生可展示结果；只有 passed 且来源校验合格可自动记忆。失败/取消不计质量通过。

## 核心数据流

任务创建：鉴权/幂等键 → 请求校验与文档选择 → queued 落库 → 受控领取 → 模板/历史结构规划 → 网络和上传文档 Evidence → 预算化写作 → 引用校验 → 审核/返工 → 结果/质量独立落库 → 合格才 remember。

每个 Evidence 保留来源、片段、时间和任务归属；文档原件删除后旧任务保留当时证据快照并标识来源已删除。对外页面只展示授权内容。失败流保留 request_id 与错误码，日志脱敏，不把失败的技术文本当报告正文。

## 契约、错误与兼容

新接口采用 /api/v2，成功 {data,meta:{request_id}}，失败 {error:{code,message,retryable,request_id}}。HTTP 语义：202 入队、400 业务输入、401 未认证、403 无权限、404 不存在、409 状态冲突/幂等冲突、413 文件过大、422 Schema、429 容量或预算限制、503 依赖不可用。

v1 保留现有响应形状作兼容适配，前端按模块逐步切换 v2。新增任务状态同步老客户端类型与回归；历史布尔 passed 不再代表可验证质量。

## 数据与迁移

任务结果、来源、检索记录、用量、评测运行版本持久化；候选表为 task_sources、task_retrievals、model_calls、schema_migrations，具体 SQL 在对应 SDD 开工前细化。外键、索引、JSON 白名单和幂等约束必须有测试。

DATA_DIR 统一保存业务库/检查点/向量库/上传资料/非密钥配置；预置模板作为只读资源。schema_version 与 code_tag 进入备份清单。Git 回滚不恢复运行数据。

## 运行与成本约束

单 worker、有限并发；不能宣称进程内队列具备分布式语义。模型调用记录真实 usage，不可用则 unknown；价格为空则费用为空。上下文按证据逐条裁剪，输出 Token 预留，明确重试与取消传播。来源文本不可信，脚本执行权限独立于模型判断。
