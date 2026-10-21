<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { fetchTaskUsage, type UsageReport } from '../api/tasks'

const props = defineProps<{ taskId: string; running?: boolean }>()
const report = ref<UsageReport | null>(null)
const loading = ref(false)
const error = ref('')
let generation = 0
let timer: ReturnType<typeof setInterval> | null = null
const labels = { running: '调用中', success: '成功', failed: '失败', cancelled: '已取消', budget_exceeded: '预算不足，调用前阻止' }

function stop() { if (timer) clearInterval(timer); timer = null }
async function load() {
  const id = props.taskId, version = ++generation
  loading.value = true
  error.value = ''
  try {
    const result = await fetchTaskUsage(id)
    if (version !== generation || id !== props.taskId) return
    report.value = result
    stop()
    if (props.running) timer = setInterval(() => { if (!loading.value) void load() }, 2000)
  } catch {
    if (version !== generation || id !== props.taskId) return
    report.value = null
    error.value = '用量记录暂不可用，请重试'
    stop()
  } finally {
    if (version === generation) loading.value = false
  }
}
function download() {
  if (!report.value) return
  const url = URL.createObjectURL(new Blob([JSON.stringify(report.value, null, 2)], { type: 'application/json' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `usage-${encodeURIComponent(props.taskId)}.json`
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
watch(() => props.taskId, () => { generation++; report.value = null; error.value = ''; loading.value = false; stop() })
watch(() => props.running, running => { if (!running) stop() })
onBeforeUnmount(() => { generation++; stop() })
</script>
<template>
  <section class="usage-panel" aria-label="模型用量与费用">
    <div class="usage-heading">
      <h3>模型用量与费用</h3>
      <button :disabled="loading" @click="load">{{ loading ? '读取中…' : report ? '刷新用量' : '查看用量与费用' }}</button>
      <button v-if="report" @click="download">导出用量 JSON</button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="report">
      <p v-if="!report.items.length">尚无调用记录；历史用量未知</p>
      <p>实际尝试 {{ report.call_count }} 次 · 调用前阻止 {{ report.blocked_count }} 次 · 累计耗时 {{ (report.duration_ms / 1000).toFixed(2) }} 秒</p>
      <p>输入 Token：{{ report.input_tokens ?? '未知' }} · 输出 Token：{{ report.output_tokens ?? '未知' }} · 未知用量 {{ report.unknown_calls }} 次</p>
      <p>当前 Token 预算 {{ report.budget_limit_tokens }} · 已扣账 {{ report.budget_charged_tokens }} · 剩余 {{ report.budget_remaining_tokens }}<span v-if="report.budget_estimated">（含估算预留）</span></p>
      <p v-if="!Object.keys(report.estimated_costs).length">费用未知：未配置单价或供应商未返回用量</p>
      <p v-else>{{ report.cost_complete ? '估算费用' : '已知部分估算费用（非总额）' }}：<span v-for="(cost, currency) in report.estimated_costs" :key="currency">{{ currency }} {{ cost.toFixed(8) }} </span></p>
      <p class="usage-note">Token 未知时按预留扣账；费用来自配置单价估算，不能保证严格 Token 或金额封顶。</p>
      <div v-if="report.items.length" class="usage-scroll">
        <table>
          <thead><tr><th>Agent / 模型</th><th>结果</th><th>尝试序号</th><th>耗时 ms</th><th>输入 / 输出 Token</th><th>估算费用</th></tr></thead>
          <tbody><tr v-for="row in report.items" :key="row.call_id">
            <td>{{ row.agent }} / {{ row.model }}</td><td>{{ labels[row.status] }}<span v-if="row.error_code">（{{ row.error_code }}）</span></td>
            <td>{{ row.attempt }}</td><td>{{ row.duration_ms.toFixed(1) }}</td>
            <td>{{ row.input_tokens ?? '未知' }} / {{ row.output_tokens ?? '未知' }}</td>
            <td>{{ row.estimated_cost === null ? '未知' : `${row.currency} ${row.estimated_cost.toFixed(8)}` }}</td>
          </tr></tbody>
        </table>
      </div>
    </template>
  </section>
</template>

<style scoped>
.usage-panel { padding: 20px; margin: 16px 0; border: 1px solid #e4e7ed; border-radius: 8px; background: white; color: #303133; }
.usage-heading { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
h3 { margin: 0 auto 0 0; font-size: 16px; }
button { padding: 7px 12px; border: 1px solid #dcdfe6; border-radius: 4px; background: white; color: #245bc2; cursor: pointer; }
button:disabled { opacity: .6; cursor: wait; }
.usage-note { color: #606266; font-size: 13px; }
.usage-scroll { overflow-x: auto; max-height: 360px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { padding: 10px; text-align: left; border-bottom: 1px solid #ebeef5; overflow-wrap: anywhere; }
th { white-space: nowrap; background: #f5f7fa; }
[role="alert"] { color: #b42318; }
</style>
