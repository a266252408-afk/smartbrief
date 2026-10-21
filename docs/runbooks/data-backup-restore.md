# 本地停写备份与副本恢复

T10本地工具；Docker卷重建和恢复按用户2026-10-21要求暂缓，本文命令不操作Docker。普通备份不包含.env，环境密钥另行安全保存。

## 备份

先停止SmartBrief后端及其MCP写入，确认没有在途任务或评测。`-Stopped`记录所有者的停写确认；脚本不会自动杀进程。同一业务库实例锁会拒绝活动SmartBrief服务，但不能阻止不遵守应用锁的外部编辑器。

从仓库根目录运行，所有路径使用绝对路径；以实际读取版本的代码标签替换示例：

```powershell
./scripts/backup-data.ps1 -DataDir 'D:/SmartBriefData' -Destination 'D:/Backups/SmartBrief-20261021' -CodeTag 'sb-T10-verified' -Stopped
if ($LASTEXITCODE -ne 0) { throw '备份失败，禁止迁移或切换数据' }
```

未设置DATA_DIR的旧本地实例，DataDir为仓库的backend绝对路径。脚本仅复制业务库/检查点及WAL、SHM、完整data/chroma_db、data/uploads、skills_pool与三个非密钥运行配置；模板/术语来自代码，不从数据副本冒充新版本资源。忽略缓存与.env；拒绝链接、穿越和源/目标重叠，备份目标必须不存在。

成功清单manifest.json记录清单版本、业务版本、代码标签、上海时间、一致性方式、每个文件大小/SHA256、SQLite完整性/表行数和集合数量。已有向量在独立校验副本中自查询，不下载模型或发起嵌入请求；这只能证明恢复检索链路，不能证明语义质量。清单最后写入；失败目标保留以便排查，不应启用，也不会自动删除。

## 恢复到新目录

```powershell
./scripts/restore-data.ps1 -BackupDir 'D:/Backups/SmartBrief-20261021' -Destination 'D:/SmartBriefData-Restored' -CodeTag 'sb-T10-verified'
if ($LASTEXITCODE -ne 0) { throw '恢复失败，禁止启用目标' }
```

先验证完整备份再建立全新目标，重复恢复到同一目录会拒绝，不覆盖原实例。校验文件哈希、清单成员、SQLite行数和向量查询，再输出成功。清单不是签名：只恢复所有者信任的本地备份，文件哈希用于发现损坏与错配。普通配置内容不输出到日志。

CodeTag必须与清单完全匹配，业务数据版本不得高于当前代码支持值。旧代码没有新迁移保护时，严禁直接连接新schema；使用与旧代码匹配的备份副本。恢复工具不自动运行迁移、不改变任务状态、不自动重试/恢复任务、不修改DATA_DIR或启动服务。

验证报告读取、上传检索、记忆准入、检查点和配置后，再由所有者切换DATA_DIR并重启。升级会由启动迁移执行，必须保留原始备份。代码回滚不回滚数据，沿用[Git与数据回滚手册](git-and-data-rollback.md)。本地隔离测试已覆盖实际PowerShell入口、独立进程业务重读与失败链路；容器验收未完成。

## Python入口

Linux/macOS或直接调用Python时使用项目虚拟环境，参数相同：

```text
.venv/bin/python scripts/data_backup.py backup --source /absolute/data --destination /absolute/backup --code-tag sb-T10-verified --stopped
.venv/bin/python scripts/data_backup.py restore --source /absolute/backup --destination /absolute/restored --code-tag sb-T10-verified
```

跨平台代码路径可用不表示已在其他系统执行验收；本次成绩来自Windows。
