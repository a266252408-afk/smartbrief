<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { authRole } from '../api/session'
import { fetchLiveConfig, type LiveConfig, type ModelConsent, type SearchConsent } from '../api/evaluationCenter'
const props = defineProps<{ kind: 'model' | 'search' | 'judge'; caseCount: number; disabled?: boolean; busy?: boolean }>()
const emit = defineEmits<{ launch: [consent: ModelConsent | SearchConsent] }>()
const config = ref<LiveConfig | null>(null), error = ref(''), loading = ref(false), confirmed = ref(false)
const calls = ref(props.kind === 'judge' ? 10 : 40), inputBytes = ref(60000), outputTokens = ref(4000)
const inputPrice = ref<number | undefined>(), outputPrice = ref<number | undefined>(), searchPrice = ref<number | undefined>()
const ceiling = ref<number | undefined>(), currency = ref<'USD' | 'CNY'>('CNY')
const id = ref('ui_' + crypto.randomUUID().replace(/-/g, ''))
const ready = computed(() => !!config.value?.[props.kind]?.ready)
const estimate = computed(() => props.kind === 'search'
  ? props.caseCount * (searchPrice.value || 0)
  : props.caseCount * calls.value * (inputBytes.value * (inputPrice.value || 0) + outputTokens.value * (outputPrice.value || 0)) / 1000000)
const valid = computed(() => ready.value && props.caseCount > 0 && !props.disabled && confirmed.value && ceiling.value! > 0
  && (props.kind === 'search' ? searchPrice.value! > 0 : inputPrice.value! > 0 && outputPrice.value! > 0)
  && estimate.value <= ceiling.value!)
watch(() => [props.caseCount, calls.value, inputBytes.value, outputTokens.value, inputPrice.value, outputPrice.value, searchPrice.value, ceiling.value, currency.value], () => { confirmed.value = false })
async function load() {
  loading.value = true; error.value = ''; confirmed.value = false
  try { config.value = await fetchLiveConfig() }
  catch { config.value = null; error.value = '真实评测配置读取失败，请重试' }
  finally { loading.value = false }
}
function launch() {
  if (!valid.value || props.busy || authRole.value !== 'admin' || !config.value) return
  const common = { confirmed: true, authorization_id: id.value, dataset_hash: config.value.dataset_hash, estimated_cost_ceiling: ceiling.value! }
  const consent = props.kind === 'search' ? { ...common, per_call_estimate_ceiling: searchPrice.value! }
    : { ...common, model: (props.kind === 'judge' ? config.value.judge! : config.value.model).name, base_url: (props.kind === 'judge' ? config.value.judge! : config.value.model).base_url, max_model_calls: calls.value,
      max_input_bytes: inputBytes.value, max_output_tokens: outputTokens.value, input_price_per_million: inputPrice.value!,
      output_price_per_million: outputPrice.value!, currency: currency.value }
  emit('launch', consent)
}
onMounted(load)
</script>

<template>
  <section class="live-launch" :aria-label="kind === 'judge' ? '真实独立评分设置' : kind === 'model' ? '真实模型评测设置' : '真实搜索评测设置'">
    <h4>{{ kind === 'judge' ? '真实独立评分' : kind === 'model' ? '真实模型评测' : '真实搜索评测' }}</h4>
    <p v-if="loading" role="status">正在读取当前配置…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <el-button :loading="loading" @click="load">刷新配置</el-button>
    <template v-if="config">
      <p v-if="kind !== 'search'">当前模型：{{ (kind === 'judge' ? config.judge : config.model)?.name || '未配置' }}；数据发送至 {{ (kind === 'judge' ? config.judge : config.model)?.base_url || '未配置' }}。</p>
      <p v-else>公开查询发送至 {{ config.search.base_url }}，每案例一次请求，最多5条摘要，不自动重试。</p>
      <p v-if="!ready" role="alert">{{ kind !== 'search' ? '模型名称、HTTPS端点或API Key未正确配置，请在后端本地配置后刷新。' : 'EXA API Key未配置，请在后端本地配置后刷新。' }}</p>
      <p v-if="kind === 'model'">使用当前主模型实际执行，发送所选公开题干、资料及模型生成上下文；搜索使用固定资料，长期记忆隔离。独立质量另行评判。</p>
      <p v-if="kind === 'model'">完整任务每案例至少 20 次模型调用，建议 40 次；低于 20 次会在改写前耗尽预算，服务端直接拒绝。</p>
      <p v-if="kind === 'judge'">单独调用评分器检查给定报告/结果及参考资料，结论不是人工核验；本次次数和费用为整批上限（校准为10样本）。</p>
      <p v-if="kind === 'judge' && config.judge?.source === 'backup'">评分器使用备用模型，与主模型独立；模型判定仍需人工抽查，不等于事实准确率。</p>
      <p v-else-if="kind === 'judge' && config.judge?.source === 'dedicated'">评分器使用独立配置模型；模型判定仍需人工抽查，不等于事实准确率。</p>
      <p v-else-if="kind === 'judge' && config.judge?.same_as_primary">评分器沿用当前主模型，独立调用仍可能存在同模型偏差，建议人工抽查或配置不同评分模型。</p>
      <fieldset :disabled="authRole !== 'admin' || busy">
        <template v-if="kind !== 'search'">
          <label>{{ kind === 'judge' ? '本次评分总调用上限' : '每案例模型调用上限' }} <el-input-number v-model="calls" :min="1" :max="40" :aria-label="kind === 'judge' ? '本次评分总调用上限' : '每案例模型调用上限'" /></label>
          <label>每次输入字节上限 <el-input-number v-model="inputBytes" :min="1" :max="200000" :step="1000" aria-label="每次输入字节上限" /></label>
          <label>每次输出Token上限 <el-input-number v-model="outputTokens" :min="1" :max="8000" :step="100" aria-label="每次输出Token上限" /></label>
          <label>每百万输入Token单价 <el-input-number v-model="inputPrice" :min="0" :precision="6" aria-label="每百万输入Token单价" /></label>
          <label>每百万输出Token单价 <el-input-number v-model="outputPrice" :min="0" :precision="6" aria-label="每百万输出Token单价" /></label>
          <label>费用币种 <el-select v-model="currency" aria-label="费用币种"><el-option label="人民币 CNY" value="CNY" /><el-option label="美元 USD" value="USD" /></el-select></label>
        </template>
        <label v-else>每请求估计费用上限（USD） <el-input-number v-model="searchPrice" :min="0" :precision="6" aria-label="每请求估计费用上限" /></label>
        <label>本次估计费用上限 <el-input-number v-model="ceiling" :min="0" :precision="6" aria-label="本次估计费用上限" /></label>
        <p>所选 {{ caseCount }} 例，保守预留估计 {{ estimate.toFixed(6) }} {{ kind === 'search' ? 'USD' : currency }}。请按供应商实际单价填写；此估计不是账单硬封顶，失败尝试也可能计费。</p>
        <p v-if="ceiling && estimate > ceiling" role="alert">本次费用上限不足，请减少案例/调用上限或调整预算。</p>
        <el-checkbox v-model="confirmed" :disabled="!ready" aria-label="确认本次真实调用与费用">我允许发送上述公开内容，并按本次参数调用及消费</el-checkbox>
      </fieldset>
      <p><el-button type="primary" :disabled="authRole !== 'admin' || !valid" :loading="busy" @click="launch">{{ kind === 'judge' ? '确认运行真实独立评分' : kind === 'model' ? '确认运行真实模型评测' : '确认运行真实搜索评测' }}</el-button></p>
      <p>本次编号 {{ id }}；启动后仅消费一次，失败后请先核对账单，重新运行需刷新页面生成新编号。</p>
    </template>
  </section>
</template>
<style scoped>
.live-launch{border-top:1px solid #dbe2ea;margin-top:24px;padding-top:20px;min-width:0}
.live-launch h4{font-size:16px;margin:0 0 12px}
.live-launch p{overflow-wrap:anywhere;line-height:1.7;margin:12px 0}
fieldset{border:0;margin:20px 0 0;padding:0;min-width:0;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px 24px}
fieldset > label:not(.el-checkbox){display:grid;align-content:start;gap:8px;min-width:0;line-height:1.5}
label :deep(.el-input-number),label :deep(.el-select){width:100%;min-width:0;max-width:100%}
fieldset > p,fieldset > :deep(.el-checkbox){grid-column:1/-1;min-width:0}
.live-launch :deep(.el-checkbox){height:auto;align-items:flex-start;white-space:normal;max-width:100%;margin-right:0;line-height:1.6}
.live-launch :deep(.el-checkbox__input){flex:0 0 auto;margin-top:4px}
.live-launch :deep(.el-checkbox__label){min-width:0;white-space:normal;overflow-wrap:anywhere;line-height:1.6}
.live-launch :deep(.el-button){height:auto;min-height:32px;max-width:100%;white-space:normal;line-height:1.5}
.live-launch :deep(.el-button > span){min-width:0;white-space:normal;overflow-wrap:anywhere}
@media(max-width:1000px){fieldset{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){fieldset{grid-template-columns:minmax(0,1fr);gap:16px}.live-launch{padding-top:16px}}
</style>
