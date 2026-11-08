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
