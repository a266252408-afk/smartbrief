<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { compareEvaluations, type EvalReport, type EvalReportSummary, type EvaluationComparison } from '../api/evaluation'

const props = defineProps<{ report: EvalReport; reports: EvalReportSummary[] }>()
const baseline = ref('')
const comparison = ref<EvaluationComparison | null>(null)
const error = ref('')
const busy = ref(false)
let version = 0
watch(() => props.report.eval_id, () => { version++; comparison.value = null; error.value = ''; baseline.value = ''; busy.value = false })
watch(baseline, () => { version++; comparison.value = null; error.value = ''; busy.value = false })
onBeforeUnmount(() => { version++ })
async function compare() {
  const request = ++version
  busy.value = true
  error.value = ''
  comparison.value = null
  try {
    const result = await compareEvaluations(props.report.eval_id, baseline.value)
    if (request === version) comparison.value = result
  } catch (e: any) {
    if (request === version) error.value = e?.response?.data?.error?.message || '对照读取失败，请重试'
  } finally {
    if (request === version) busy.value = false
  }
}
function number(value: number | null | undefined) { return value == null ? '未知' : String(value) }
function percent(value: number | null | undefined) { return value == null ? '未知（无引用）' : `${(value * 100).toFixed(1)}%` }
</script>

<template>
  <el-card class="protocol-card" shadow="never" data-testid="evaluation-protocol">
    <template #header><strong>{{ report.metadata ? '离线协议评测 · 合成材料' : '历史评测 · 版本信息未知' }}</strong></template>
    <template v-if="report.metadata">
      <el-alert title="合成模型、搜索与嵌入只验证软件协议，不代表在线效果；人工事实抽查与在线实验均为 pending。" type="warning" :closable="false" />
      <p>数据集 {{ report.metadata.suite_version }} · 模型 {{ report.metadata.model }} · 提示 {{ report.metadata.prompt_version }}</p>
      <p>代码 {{ report.metadata.code_tag || report.metadata.code_commit || '未知' }} · 未提交修改 {{ report.metadata.code_dirty == null ? '未知' : report.metadata.code_dirty ? '有' : '无' }}</p>
      <p class="hash">数据哈希 {{ report.metadata.dataset_hash }}<br>源码哈希 {{ report.metadata.source_hash || '未知' }}</p>
      <p>开关 RAG={{ report.metadata.feature_flags.rag }} / 记忆={{ report.metadata.feature_flags.memory }} / 审核={{ report.metadata.feature_flags.review }}</p>
      <p>{{ report.metadata.memory_protocol }}</p>
      <p>{{ report.metadata.randomness }}</p>
      <template v-if="report.metrics.quality">
        <p>质量通过率 {{ percent(report.metrics.quality.quality_pass_rate) }} · 执行完成率 {{ percent(report.metrics.quality.execution_completion_rate) }} · 待完成 {{ report.metrics.quality.pending_cases }}</p>
        <p>有效引用率 {{ percent(report.metrics.quality.citation_valid_rate) }}（仅结构映射，不证明事实真实）</p>
        <p>输入 Token {{ number(report.metrics.quality.input_tokens) }} · 输出 Token {{ number(report.metrics.quality.output_tokens) }} · P95 {{ number(report.metrics.quality.p95_completed_seconds) }} 秒</p>
        <p>{{ report.metrics.quality.cost_complete ? '完整费用估算' : '费用未知或部分已知' }}：{{ Object.keys(report.metrics.quality.estimated_costs).length ? JSON.stringify(report.metrics.quality.estimated_costs) : '未知' }}（不同币种分列）</p>
        <p>失败分类：{{ JSON.stringify(report.metrics.quality.failure_categories) }}</p>
      </template>
      <div class="compare-controls">
        <el-select v-model="baseline" placeholder="选择同配置的基线评测" aria-label="对照基线" style="width: 280px">
          <el-option v-for="item in reports.filter(item => item.eval_id !== report.eval_id && item.status === 'completed')" :key="item.eval_id" :value="item.eval_id" :label="item.eval_id" />
        </el-select>
        <el-button data-testid="compare-evaluations" :disabled="!baseline || busy || report.status !== 'completed'" :loading="busy" @click="compare">比较单个开关</el-button>
      </div>
      <p v-if="error" role="alert" data-testid="compare-error">{{ error }}</p>
      <p v-if="comparison">开关 {{ comparison.changed_feature }}，质量通过率差值 {{ (comparison.quality_pass_rate_delta * 100).toFixed(1) }} 个百分点。{{ comparison.interpretation }}</p>
    </template>
    <p v-else>旧版结果不补造数据集、模型或源码版本，不参与新版对照。</p>
  </el-card>
</template>

<style scoped>
.protocol-card { margin-bottom: 16px; }
p { font-size: 13px; line-height: 1.8; overflow-wrap: anywhere; }
.hash { color: #606266; }
.compare-controls { display: flex; gap: 12px; flex-wrap: wrap; }
</style>
