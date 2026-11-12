# SmartBrief 评测数据包 v1

冻结日期：2026-10-31（Asia/Shanghai）。这是用户授权创建的完整首版数据包，覆盖前述方案五板块；不是对所有未来领域和所有故障的穷尽。

## 数量和入口

- 黄金集24例：官方资料支撑的搜索/写作16例，跨资料完整任务4例，协议/恢复基线4例。
- 回归集24例：历史修复和已明确的保护契约。既有测试引用可定位，不宣称每例都是发生过的线上事故。
- 挑战集40例：资料缺口、过期、冲突、注入、口径诱导、主备/恢复/计量及组合边界。
- 旧protocol-1完整保留20例，原应用数据文件未修改。合计108条题目记录，不代表108个独立研究主题。
- 另外提供10条评分器校准样本，不计入108例。
- 8张官方来源事实卡、23个参考事实、14类评分标准、68份合成场景定义。

从[两个抽查案例](REVIEW.md)开始；全量人工可读内容见[案例目录](CASEBOOK.md)。自动化输入见public/，完整标准见private/。

## 文件组织

| 文件 | 内容 | 是否提供给被测Agent |
| --- | --- | --- |
| public/golden.json、regression.json、challenge.json | 用户任务及运行限制 | 是，但不同模式仍由适配器装配 |
| private/同名JSON | 原始任务、隐藏参考事实ID、逐项断言、来源/场景关联 | 否，仅执行器和评判器读取 |
| sources/S01～S08.json | 官方URL、日期、原文位置、事实、口径、核对状态 | 整张卡不提供，含题目设计 |
| evidence-documents.json | 可用于固定资料模式的短事实转述 | 固定资料模式可给；联网搜索模式不预给 |
| fixtures.json＋fixture-protocol.json | 完整合成场景和默认条件、事件语义、隔离规则 | 由适配器控制，不能当真实运行证据 |
| rubrics.json | 评分标准、方法、状态 | 给评判器，不泄漏给被测Agent |
| calibration.json | 正确/错年份/错对象/预测实绩等判分样本 | 开发子集可校准；evaluation_only只能最终检验 |
| legacy/protocol-1.json | 旧20例数据副本 | 仅旧协议适配器 |
| manifest.json、coverage.json | 文件指纹、覆盖统计和版本 | 管理与校验 |
| acquisition-audit.json | 来源获取范围及被排除的材料 | 审计 |
| tools/build_pack.py、validate_pack.py | 可复现生成和独立校验 | 不调用模型、不联网 |

## “黄金”与人工抽查状态

黄金表示有固定参考事实或确定性行为oracle，不表示已经人工验收。所有案例human_review=pending；由Codex核对官方页面或仓库契约。用户抽查1～2例后，只修改这些样本的人工状态，不能把全部案例自动标成人工通过。

参考数据是截至冻结日对指定资料的核对，不保证整个互联网没有更新资料。S02是滚动页面，后续联网评测必须保存当次证据。S05精确发布日期未确认，保留null。来源卡为简短事实转述，不是网页全文或原始HTTP字节快照；SHA256是本地卡片指纹。

搜索参考URL不是唯一正确URL。等价可信来源可以得分；统计年份、地区、定义和预测/实绩必须一致。不得从公开网页的摘要推断未展示的付费报告内容。

## 判分规则

逐项pass/fail/unknown/not_applicable；所有适用关键项通过才判本例处理正确。未知、评判器超时和未执行不算通过。报告质量单列：合理拒绝或暂停可以处理正确，不能算完成了一份合格报告。

数字允许等价单位转换，必须保留约/超过/接近等限定。不要用全文关键词或精确字符串当事实评分。同比与环比、增长百分比与百分点、存量与年度新增、全球与中国、出货量与零售额不能互换。

程序判定路由/状态/预算/隔离；独立模型辅助证据支持和需求覆盖；用户复核争议和少量随机样本。生产Reviewer不是最终外部评判器。

## 分组与防止答案泄漏

wearables、ev、connectivity、robotics为开发资料组；china_macro、data_centres为留出组。同一资料及变体不跨入开发。跨组完整任务单列evaluation_only，不能作为独立未见主题扩大统计。校准J06/J09/J10含留出资料，标evaluation_only，不能用于调评分提示。

这些文件在本地都可读，因此留出依赖实施流程隔离，不宣称技术上不可访问。联网任务仅传公开题干；冻结资料任务只传evidence-documents中的内容，不传oracle、禁答清单、来源卡的task字段或待测标准。

## 执行边界与现有系统兼容

数据准备完成，88例新题尚未接入当前评测页面，application_runner_support=not_integrated；不得直接把本包丢给仅支持C01～C20的旧loader。题目、资料和故障定义可供后续适配器使用，但本轮没有建设/运行新执行器。

真实搜索、模型或EXA调用均未由本包默认授权；用户授权的本轮公开网页检索已用于资料准备。字段network_authorized=false、paid_calls_authorized=false是未来执行默认限制，不是否认本次浏览授权。

路由故障专项必须走真实routed_chat、只替换SDK边界；不能置入现有合成evaluation_context绕过路由。过程测试在D盘独立数据库/检查点/向量目录；强杀只针对测试子进程，不操作用户服务。恢复以已落盘单元为界，外部请求成功但检查点未提交时允许重放中断单元。

fixture事件为描述性数据，非已经执行的轨迹。`response=valid`由后续适配器按调用角色生成符合语法的合成响应；不自动代表质量passed。所有未指定SDK返回采用fixture-protocol默认值；未匹配的回放查询必须显式失败，不凭oracle造答案。场景特定值覆盖默认值。现有测试引用表示相关依据，不代表可以直接一一运行同一参数。

## 校验与重建

在仓库根目录运行：

```powershell
python docs/evaluation-datasets/v1/tools/validate_pack.py
```

维护定义后重建，再校验：

```powershell
python docs/evaluation-datasets/v1/tools/build_pack.py
if ($LASTEXITCODE -ne 0) { throw '生成失败' }
python docs/evaluation-datasets/v1/tools/validate_pack.py
if ($LASTEXITCODE -ne 0) { throw '校验失败' }
```

validate检查文件集/指纹、ID、资料/事实/评分引用、现存测试函数、五板块覆盖、留出隔离和原协议一致性。它不联网复核网页，也不证明被测系统成绩。维护人工标注时应同步生成定义和版本，不能只改输出再重建覆盖标注。

所有文件存D盘；不改生产代码、公共API Schema、真实数据库和.env；不伪造评测成绩、不移动已有verified标签。
