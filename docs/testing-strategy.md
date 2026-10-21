# 测试策略与不可跳过的关卡

## 当前基线与命令前置

T00 已对现有代码新跑后端 342 个测试、所统计范围覆盖率 93%，前端类型检查与生产构建通过；记录见 verification/T00.json。T01 已固化项目 .venv 和锁文件，安装与匹配 Chromium 说明见 [环境说明](environment.md)；不把 T00 临时环境当交付环境。

后端命令从 backend/ 执行；前端从 frontend/ 执行；治理检查从根目录执行。T01 后使用项目 .venv 内的 Python（Windows：..\.venv\Scripts\python.exe，Linux/macOS：../.venv/bin/python），或者激活环境再使用 python。依赖安装命令以 T01 实际生成的锁文件为准。

## 每个任务必过

1. Schema/骨架检查：请求/响应、状态枚举、错误与旧接口兼容。
2. 模块测试：执行 implementation.md 对应的真实命令；新测试文件先落地，不能引用不存在的文件后跳过。
3. 集成测试：每个 SDD 的 AC-Txx-2，至少覆盖一个真实跨模块业务边界；付费模型只替换外部边界，不 mock 掉被测业务。
4. 触及前端：npm run test:unit -- --run + npm run build；触及主流程再跑 npm run test:e2e。没有触及前端的任务在凭据中写明适用性，不把“没跑”记成通过。
5. 新增或修改 Python：python -m ruff check .（T01 建立后）；治理检查和 Git diff --check 始终执行。
6. AC 逐条验证后，日志、状态和 Git snapshot 同步；任何必要项失败，不进入下一任务。

每个可独立验收功能子项重复上述步骤并独立快照。父任务不能把前一个子项的测试结果沿用为后一个子项的成绩。

## 用例矩阵

| 边界 | 必测情形 |
| --- | --- |
| 权限 | 无 token、错误 token、只读写操作、管理员正常、日志脱敏 |
| 文件 | ../、绝对路径、Windows 驱动器/反斜杠、超限、非法格式、同名文件、ZIP 路径穿越 |
| 质量 | 严格布尔解析、非 JSON、缺字段、LLM 错误、空稿、返工上限、旧记录 unknown |
| 证据 | 引用缺失/不存在/重复、非 HTTP URL、来源被删除、重写丢引用、注入指令 |
| RAG | 独有事实、无命中、低分、选文档隔离、删除与旧证据、嵌入失败 |
| 预算 | usage 缺失、失败重试、多任务隔离、调用前阻止、取消后不再调用 |
| 任务 | 并发上限、排队取消、运行取消、取消与完成竞争、恢复幂等、进程重启 |
| 数据 | 旧 schema 升级、二次迁移、备份校验失败、恢复到新目录、容器重建 |

## T13 最终全量

从干净环境按锁文件安装，不沿用 T00 临时依赖。后端执行 implementation.md 的 T13 命令（测量 agents/llm/tools/mcp_client/api/skills/mcp_servers/db/graph/rag/memory/evaluation/services）；前端执行 npm run test:unit -- --run、npm run test:e2e、npm run build；后端 python -m ruff check .；根目录 python scripts/verify_construction.py 与 git diff --check。容器验收运行 T10 生成并已验证的脚本，隔离临时卷，不访问用户真实数据。

整体覆盖率 >=80%；新增权限/状态机/引用/预算/迁移模块分别 >=95%，用 coverage JSON 逐文件检查。关键行为必须有断言，不能只追求行覆盖率。禁止删除失败测试、降低断言、增加全局 ignore 或用无测试的成功命令替代。

在线模型效果实验不属于默认测试：需单独授权，保留数据集哈希、模型、提示版本、代码 tag、开关和真实运行记录。未执行时如实标记 pending；不得因无在线预算卡住离线编码，也不得称在线效果已验收。

## 测试凭据

每个功能写 verification/Txx.json 或 Txx-sub.json：任务、子功能、时间/时区、环境、命令/工作目录/退出码、测试数量、覆盖范围、日志路径、源代码指纹、AC 结果和预期快照标签。stdout/stderr 可保留经脱敏的 txt；API Key、文档全文和生产数据不进入仓库。测试后变更被测源码必须重跑适用关卡。

## 2026-10-21 Docker范围授权

用户明确将Docker工作延后至项目完成后再决定。T10/T10-03/T13本轮container_restore移入state.json.deferred_work保持pending，不视为门禁通过；T10-02亦为延后项。当前验收只覆盖本地链路，允许完成本地快照后继续后续阶段。恢复Docker工作需用户后续明确决定；本地结果不证明容器行为。其余适用门禁保持不变。
