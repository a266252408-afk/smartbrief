<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { authRole } from '../api/session'
import { fetchLiveConfig, type LiveConfig, type ModelConsent, type SearchConsent } from '../api/evaluationCenter'
const props = defineProps<{ kind: 'model' | 'search'; caseCount: number; disabled?: boolean; busy?: boolean }>()
const emit = defineEmits<{ launch: [consent: ModelConsent | SearchConsent] }>()
const config = ref<LiveConfig | null>(null), error = ref(''), loading = ref(false), confirmed = ref(false)
const calls = ref(20), inputBytes = ref(60000), outputTokens = ref(4000)
const inputPrice = ref<number | undefined>(), outputPrice = ref<number | undefined>(), searchPrice = ref<number | undefined>()
const ceiling = ref<number | undefined>(), currency = ref<'USD' | 'CNY'>('CNY')
const id = ref('ui_' + crypto.randomUUID().replace(/-/g, ''))
const ready = computed(() => !!config.value?.[props.kind].ready)
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
    : { ...common, model: config.value.model.name, base_url: config.value.model.base_url, max_model_calls: calls.value,
      max_input_bytes: inputBytes.value, max_output_tokens: outputTokens.value, input_price_per_million: inputPrice.value!,
      output_price_per_million: outputPrice.value!, currency: currency.value }
  emit('launch', consent)
}
onMounted(load)
</script>

<template>
  <section class="live-launch" :aria-label="kind === 'model' ? '真实模型评测设置' : '真实搜索评测设置'">
    <h4>{{ kind === 'model' ? '真实模型评测' : '真实搜索评测' }}</h4>
    <p v-if="loading" role="status">正在读取当前配置…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <el-button :loading="loading" @click="load">刷新配置</el-button>
    <template v-if="config">
      <p v-if="kind === 'model'">当前模型：{{ config.model.name || '未配置' }}；数据发送至 {{ config.model.base_url || '未配置' }}。</p>
      <p v-else>公开查询发送至 {{ config.search.base_url }}，每案例一次请求，最多5条摘要，不自动重试。</p>
      <p v-if="!ready" role="alert">{{ kind === 'model' ? '模型名称、HTTPS端点或API Key未正确配置，请在后端本地配置后刷新。' : 'EXA API Key未配置，请在后端本地配置后刷新。' }}</p>
      <p v-if="kind === 'model'">使用当前主模型实际执行，发送所选公开题干、资料及模型生成上下文；搜索使用固定资料，长期记忆隔离。独立质量另行评判。</p>
      <fieldset :disabled="authRole !== 'admin' || busy">
        <template v-if="kind === 'model'">
          <label>每案例模型调用上限 <el-input-number v-model="calls" :min="1" :max="40" aria-label="每案例模型调用上限" /></label>
          <label>每次输入字节上限 <el-input-number v-model="inputBytes" :min="1" :max="200000" :step="1000" aria-label="每次输入字节上限" /></label>
          <label>每次输出Token上限 <el-input-number v-model="outputTokens" :min="1" :max="8000" :step="100" aria-label="每次输出Token上限" /></label>
          <label>每百万输入Token单价 <el-input-number v-model="inputPrice" :min="0" :precision="6" aria-label="每百万输入Token单价" /></label>
          <label>每百万输出Token单价 <el-input-number v-model="outputPrice" :min="0" :precision="6" aria-label="每百万输出Token单价" /></label>
          <el-select v-model="currency" aria-label="费用币种"><el-option label="人民币 CNY" value="CNY" /><el-option label="美元 USD" value="USD" /></el-select>
        </template>
        <label v-else>每请求估计费用上限（USD） <el-input-number v-model="searchPrice" :min="0" :precision="6" aria-label="每请求估计费用上限" /></label>
        <label>本次估计费用上限 <el-input-number v-model="ceiling" :min="0" :precision="6" aria-label="本次估计费用上限" /></label>
        <p>所选 {{ caseCount }} 例，保守预留估计 {{ estimate.toFixed(6) }} {{ kind === 'search' ? 'USD' : currency }}。请按供应商实际单价填写；此估计不是账单硬封顶，失败尝试也可能计费。</p>
        <p v-if="ceiling && estimate > ceiling" role="alert">本次费用上限不足，请减少案例/调用上限或调整预算。</p>
        <el-checkbox v-model="confirmed" :disabled="!ready" aria-label="确认本次真实调用与费用">我允许发送上述公开内容，并按本次参数调用及消费</el-checkbox>
      </fieldset>
      <p><el-button type="primary" :disabled="authRole !== 'admin' || !valid" :loading="busy" @click="launch">{{ kind === 'model' ? '确认运行真实模型评测' : '确认运行真实搜索评测' }}</el-button></p>
      <p>本次编号 {{ id }}；启动后仅消费一次，失败后请先核对账单，重新运行需刷新页面生成新编号。</p>
    </template>
  </section>
</template>
<style scoped>
.live-launch{border-top:1px solid #dbe2ea;margin-top:16px;padding-top:16px}.live-launch p{overflow-wrap:anywhere}fieldset{border:0;padding:0;display:grid;gap:12px}label{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}fieldset .el-select{max-width:220px}
</style>
