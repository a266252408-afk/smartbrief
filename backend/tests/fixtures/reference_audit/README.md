# 引用审核离线回归夹具（REFERENCE-AUDIT-01 Phase 0）

本目录固化两起真实失败任务的证据、草稿与审核记录，供 `services/citations.py`、
`services/numbers.py` 及相关审核/返工逻辑做离线回归。测试只读取 JSON，不调用模型、
搜索或网络。

## 文件

| 文件 | 来源任务 | 主题 |
|---|---|---|
| `smart_fridge.json` | `task_511ec6350fbe4289b2a9e051cd14286c` | 2026智能冰箱的市场调研 |
| `ebike.json` | `task_523d1c9b68174b1e8c7a226ee1c5b81d` | 2026电瓶车的市场调研 |

## 采集方式

2026-11-04 从 `backend/data.db` 的 `tasks` 表读取以下字段，按原值写入 JSON：

- `evidence`：完整来源列表（含 `task_id`，便于 `validate_sources` 校验）；
- `draft_content` / `final_report`：落库草稿与最终稿（两例当前相同）；
- `review_history`、`review_comments`、`citation_errors`：生产运行记录；
- `execution_status`、`quality_status`、`error_code`、`forced_output`、`iteration`。

夹具中的 `recorded` 是当时生产结果，不是当前代码结果；`known_cases` 是从这些记录与
来源原文整理出的判定基线，`known_gaps` 是已知尚未覆盖的检查缺口。

## 基线事实

### smart_fridge（`quality_status=rejected`，`CITATION_INVALID`）

- 数据库 `citation_errors` 实际包含 17 条数字绑定错误 + 4 条“返工丢失原引用”。
  交接文档曾记 18 条绑定错误，以数据库记录为准。
- 17 条绑定错误全部属于“引用挂错”：被引来源摘要不含这些数字，但数字能在其他来源中
  找到。夹具 `known_cases` 逐条保存候选来源，防止把挂错误判成编造。
- 其中 16 条在当前检查器下已不再误报；唯一残留是核心结论段
  `[S14] CR5 88%`，实际出处为 `S31`，可由 `rebind_citations` 确定性重挂。

### ebike（`quality_status=rejected`，`CITATION_UNBOUND`）

- `[S8] 544.4亿、1451亿` 是繁体“億”与千分位导致的旧误判；当前 `numbers.py`
  已识别，不得再报 `unbound_number`。
- `[S11] 46.39亿、74.98亿` 是真实单位错误：来源为 `USD 46.39 billion / 74.98 billion`，
  差 10 倍。检查器只报 `scale_matches`，**不自动改数字**。
- 同段 `59.49亿美元、161.46亿美元` 所在句子没有 `[S8]`。B4 段落级绑定兜底
  已能检出这两个数字，并给出相对 S8 的 10 倍量级提示；对应测试为
  `test_ebike_uncited_number_sentence_is_detected_with_scale_hint`。
  同句的 10.5% 能在 S8 中找到，不进入 `unbound_numbers`。
- writer 原稿的双一级标题、缺少“结论与建议”、2036 写成 2035、12.6% 与 13% 合并
  只存在于 `review_history` 的审核意见中，原始草稿未落库，无法做正文级重放。

## 更新规则

- 不得为了让测试通过而修改 `recorded` 或来源摘要；需要新增事实时追加字段并注明来源。
- Phase 1 起，`known_gaps` 中的缺口应逐项转为 `known_cases` 并补充正面断言。
- 夹具包含公开网页摘录，仅用于本地回归；不要把这些文件用于对外分发。
