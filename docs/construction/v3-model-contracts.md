# v3 模型内容契约与程序契约对照

R03-02本地实现；默认任务仍v2，v3仅受控metadata选定。真实主模型完成率由R07另行授权验证。

| 位置 | 模型提供 | 程序维护 |
| --- | --- | --- |
| 文档 | title、sections | version=3、task_id、revision、注册表版本、来源快照 |
| 章节 | role、title、blocks | section_id与固定摘要/结论标题、章节顺序 |
| 内容块 | kind、text、claims | block_id、是否存在未绑定数值/排名、合法块保留 |
| 声明 | quote、facts短编号、premises短编号 | claim_id、唯一原文span、精确内部fact_ids、premise_ids、source_ids |
| 局部纠正 | 提供的B1/B2短目标与value内容块 | 短目标到本次不可变块位置映射、范围检查、最多一次纠正 |
| 诊断 | 不要求自报成功、计数或哈希 | 原响应/长度/截断标记/SHA256、冻结别名上下文、实际应用决定、文档revision/hash |

输入ModelDocument与内部ReportDocument分离。结构化输出schema只发送模型最小输入字段；内部身份、长事实哈希、字符偏移和程序验收结果不要求模型填写。程序按同一冻结事实上下文精确解析F1/F2，未知或跨上下文编号拒绝，不按相似度猜测。

正文局部失败时只发送被拒绝的内容块及原因，并只接受对应短目标；其他块保持原字节。根JSON/结构无效只能进行一次规范对象纠正，仍失败不回退旧Markdown、不自动删句。已有有效文档保留；首稿无有效文档时failed/not_run并保存诊断，不能因没有有效finding就记passed。

这些是离线行为保证，不能代表主模型已学会契约。质量仍须独立审核，程序构建成功与质量通过是两个事实。

## R03-03 返工、恢复与导出

局部输出actions[{target,operation:replace/remove,value}]，短目标由程序映射到稳定block_id；value为ModelBlock或明确null，不要求模型生成digest/revision/回执。程序核对冻结的任务、修订号、完整文档指纹、短目标和事实上下文；版本/范围不能由模型豁免。局部逐条接受，全文ModelDocument一次整体接受；没有实际修改不记进展，事实覆盖减少拒绝并保留原因，进一步核心覆盖见R05。

v3审阅和报告导出按同一ReportDocument重渲染；代码示例与来源附录不作为正文事实，保留段落坐标。恢复核对检查点pipeline与任务metadata一致并验证文档；不悄悄换版本。API v2 POST /api/v2/tasks与v1 POST /api/v1/generate支持显式report_pipeline_version:3，缺省2；SDK submitTask第三参数可选3，现有调用不变。v3参与幂等请求身份。运行环境仍需停写备份后另行迁移，施工没有重启服务或迁移真实库。
