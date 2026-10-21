<script setup lang="ts">
/** 评测中心：运行评测 → 进度条 → 五维雷达图 + 指标卡 + 用例明细 + 历史对比 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import * as echarts from 'echarts'
import EvaluationProtocol from '../components/EvaluationProtocol.vue'
import { authRole } from '../api/session'
import {
  runEvaluation,
  fetchEvalReports,
  fetchEvalReport,
  deleteEvalReport,
  type EvalReport,
  type EvalReportSummary,
  type EvalCaseResult
} from '../api/evaluation'

// ---------------- 状态 ----------------

const running = ref(false)          // 当前是否有评测在执行（含后台）
const starting = ref(false)         // 正在触发评测
const reports = ref<Awaited<ReturnType<typeof fetchEvalReports>>['items']>([])
const selectedId = ref('')          // 当前选中的评测 id
const report = ref<EvalReport | null>(null)
const loading = ref(false)
const pollTimer = ref<number | null>(null)
const featureFlags = ref({ rag: true, memory: true, review: true })
const listError = ref(false)
let detailVersion = 0
let disposed = false

async function loadList() {
  try {
    const result = await fetchEvalReports({ page: 1, page_size: 20 })
    if (disposed) return
    listError.value = false
    reports.value = result.items
    // 列表里有执行中的评测 → 自动选中并轮询
    const active = result.items.find((r) => r.status === 'running')
    if (active) {
      running.value = true
      select(active.eval_id)
    } else if (result.items.length && !report.value) {
      // 默认展示最近一次评测
      select(result.items[0].eval_id)
    }
  } catch {
    listError.value = true
    ElMessage.error('获取评测历史失败，请检查后端服务')
  }
}

async function loadDetail(evalId: string) {
  const version = ++detailVersion
  loading.value = true
  try {
    const result = await fetchEvalReport(evalId)
    if (disposed || version !== detailVersion || evalId !== selectedId.value) return
    report.value = result
    running.value = report.value.status === 'running'
    if (running.value) startPolling()
    else stopPolling()
  } catch (e: any) {
    if (!disposed && version === detailVersion) ElMessage.error(e?.response?.data?.error?.message || '获取评测详情失败')
  } finally {
    if (!disposed && version === detailVersion) loading.value = false
  }
}

function select(evalId: string) {
  selectedId.value = evalId
  void loadDetail(evalId)
}

function onRowClick(row: { eval_id: string }) {
  select(row.eval_id)
}

// ---------------- 删除历史评测 ----------------

async function confirmDelete(row: EvalReportSummary) {
  try {
    await ElMessageBox.confirm(
      `确定删除评测报告 ${row.eval_id}（${row.completed_cases}/${row.total_cases}）吗？该操作不可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await deleteEvalReport(row.eval_id)
    ElMessage.success(`已删除评测报告 ${row.eval_id}`)
    // 当前选中的正是被删除的评测 → 清空选中，列表刷新后自动展示最新一条
    if (selectedId.value === row.eval_id) {
      selectedId.value = ''
      report.value = null
      stopPolling()
      running.value = false
    }
    await loadList()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '删除失败，请检查后端服务')
  }
}

// ---------------- 轮询（执行中 2s 刷新） ----------------

function startPolling() {
  stopPolling()
  pollTimer.value = window.setInterval(() => {
    if (selectedId.value) void loadDetail(selectedId.value)
  }, 2000)
}

function stopPolling() {
  if (pollTimer.value !== null) {
    window.clearInterval(pollTimer.value)
    pollTimer.value = null
  }
}

onMounted(() => {
  loadList()
})

onBeforeUnmount(() => {
  disposed = true
  detailVersion++
  stopPolling()
  radarChart.value?.dispose()
})

// ---------------- 运行评测 ----------------

async function onRun() {
  if (running.value || starting.value || authRole.value !== 'admin') return
  starting.value = true
  try {
    const result = await runEvaluation({ ...featureFlags.value })
    ElMessage.success(`离线协议评测已启动：${result.eval_id}（${result.total_cases} 例）`)
    running.value = true
    select(result.eval_id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.error?.message || '评测启动失败，请检查后端服务')
  } finally {
    starting.value = false
  }
}

// ---------------- 派生数据 ----------------

const summary = computed(() => report.value?.metrics?.summary)

/** 指标展示卡（实测值 + 目标值 + 达标标签） */
const metricCards = computed(() => {
  const s = summary.value
  if (!s) return []
  const pct = (v: number) => `${(v * 100).toFixed(1)}%`
  const target = report.value!.metrics.targets
  const met = report.value!.metrics.met
  return [
    ...((s.metric_version ?? 0) >= 2 ? [{ key: 'execution_success_rate', label: '执行完成率', value: pct(s.execution_success_rate ?? 0), target: '独立展示', met: true }] : []),
    { key: 'success_rate', label: (s.metric_version ?? 0) >= 2 ? '质量通过率' : '旧版执行成功率', value: pct(s.success_rate), target: pct(target.success_rate), met: met.success_rate },
    { key: 'avg_duration_seconds', label: '平均执行耗时', value: `${s.avg_duration_seconds.toFixed(1)}s`, target: `${target.avg_duration_seconds.toFixed(1)}s`, met: met.avg_duration_seconds },
    { key: 'p95_duration_seconds', label: 'P95 耗时', value: `${s.p95_duration_seconds.toFixed(1)}s`, target: '—', met: true },
    { key: 'avg_react_loops', label: '平均 ReAct 循环', value: `${s.avg_react_loops.toFixed(1)} 次`, target: `${target.avg_react_loops.toFixed(1)} 次`, met: met.avg_react_loops },
    { key: 'keyword_coverage_rate', label: '关键词覆盖率', value: pct(s.keyword_coverage_rate), target: pct(target.keyword_coverage_rate), met: met.keyword_coverage_rate },
    { key: 'memory_hit_rate', label: '记忆命中率', value: pct(s.memory_hit_rate), target: pct(target.memory_hit_rate), met: met.memory_hit_rate }
  ]
})

/** 雷达图数据：5 个维度统一归一化到 0~100（100 = 达到目标值口径） */
const radarOption = computed(() => {
  const s = summary.value
  if (!s) return null
  const durationScore = s.avg_duration_seconds > 0
    ? Math.max(0, Math.min(100, (1 - s.avg_duration_seconds / 120) * 100))
    : 0
  const loopScore = s.avg_react_loops > 0
    ? Math.max(0, Math.min(100, (1 - s.avg_react_loops / 8) * 100))
    : 0
  return {
    tooltip: {},
    radar: {
      indicator: [
        { name: (s.metric_version ?? 0) >= 2 ? '质量通过率' : '旧版执行成功率', max: 100 },
        { name: '关键词覆盖率', max: 100 },
        { name: '记忆命中率', max: 100 },
        { name: '耗时达标分', max: 100 },
        { name: '循环达标分', max: 100 }
      ],
      radius: '65%'
    },
    series: [
      {
        type: 'radar',
        data: [
          {
            value: [
              +(s.success_rate * 100).toFixed(1),
              +(s.keyword_coverage_rate * 100).toFixed(1),
              +(s.memory_hit_rate * 100).toFixed(1),
              +durationScore.toFixed(1),
              +loopScore.toFixed(1)
            ],
            name: '五维指标',
            areaStyle: { opacity: 0.25 }
          }
        ]
      }
    ]
  }
})

const radarChart = shallowRef<echarts.ECharts | null>(null)
const radarEl = ref<HTMLElement>()

watch([radarOption, radarEl], () => {
  if (disposed) return
  if (!radarOption.value || !radarEl.value) {
    radarChart.value?.dispose()
    radarChart.value = null
    return
  }
  if (radarChart.value && radarChart.value.getDom() !== radarEl.value) {
    radarChart.value.dispose()
    radarChart.value = null
  }
  radarChart.value ??= echarts.init(radarEl.value)
  radarChart.value.setOption(radarOption.value)
}, { flush: 'post' })

/** Markdown 报告（report_md）渲染为富文本 */
const reportMdHtml = computed(() => {
  const md = report.value?.report_md ?? ''
  if (!md) return ''
  const raw = marked.parse(md, { async: false }) as string
  return DOMPurify.sanitize(raw)
})

const showMd = ref(true)
const showCases = ref(true)

function caseStatusType(r: EvalCaseResult) {
  if (r.quality_passed === true && !r.forced_output) return 'success' as const
  if (r.quality_passed === undefined) return 'info' as const
  return 'danger' as const
}

function caseStatusLabel(r: EvalCaseResult) {
  if (r.quality_passed === undefined) return r.success ? '旧版执行完成（质量未知）' : '旧版执行失败'
  if (r.quality_passed && !r.forced_output) return '质量通过'
  return r.execution_success ? '执行完成，质量未通过' : '执行失败'
}

function coveragePercent(v: number) {
  return `${(v * 100).toFixed(1)}%`
}

function durationText(r: EvalCaseResult) {
  return r.duration_seconds == null ? '—' : `${r.duration_seconds}s`
}

function durationType(r: EvalCaseResult) {
  if (r.duration_seconds == null) return 'info' as const
  return r.duration_seconds > 120 ? 'warning' as const : 'success' as const
}

const statusMeta = {
  running: { label: '执行中', type: 'primary' as const },
  completed: { label: '已完成', type: 'success' as const },
  failed: { label: '失败', type: 'danger' as const }
}

function statusOf(status: string) {
  return statusMeta[status as keyof typeof statusMeta] ?? { label: status, type: 'info' as const }
}
</script>

<template>
  <div class="evaluation-page">
    <div class="eval-header">
      <div>
        <h2>评测中心</h2>
        <p class="hint">
          执行已配置的评测用例，分别统计执行完成率与质量通过率、耗时、行动次数、关键词覆盖率与记忆命中率。
          protocol-1 固定20例合成协议测试；在线实验需另行授权，人工事实抽查 pending。指标目标不代表实测成绩。
        </p>
      </div>
      <el-button
        type="primary"
        size="large"
        :loading="starting"
        :disabled="running || authRole !== 'admin'"
        @click="onRun"
      >
        {{ running ? '评测执行中…' : '运行离线协议评测' }}
      </el-button>
    </div>
    <div class="experiment-options">
      <el-checkbox v-model="featureFlags.rag" :disabled="running || authRole !== 'admin'">RAG</el-checkbox>
      <el-checkbox v-model="featureFlags.memory" :disabled="running || authRole !== 'admin'">长期记忆</el-checkbox>
      <el-checkbox v-model="featureFlags.review" :disabled="running || authRole !== 'admin'">审核</el-checkbox>
      <p class="hint">对照保持同一数据集、模型、提示与源码，每次只改变一个开关；固定响应的差值不能证明在线增益。</p>
    </div>
    <el-alert v-if="listError" title="评测历史读取失败，请刷新重试" type="error" :closable="false" />
    <EvaluationProtocol v-if="report" :report="report" :reports="reports" />

    <!-- 历史评测列表 -->
    <el-card shadow="never" class="eval-list-card">
      <template #header>
        <span class="card-title">历史评测</span>
        <el-tag v-if="running" type="primary" size="small" effect="plain">执行中</el-tag>
      </template>
      <el-table :data="reports" size="small" highlight-current-row @row-click="onRowClick">
        <el-table-column prop="eval_id" label="评测 ID" width="160">
          <template #default="{ row }">
            <code>{{ row.eval_id }}</code>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="开始时间" width="180" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statusOf(row.status).type" size="small">{{ statusOf(row.status).label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" min-width="140">
          <template #default="{ row }">
            <span>{{ row.completed_cases }} / {{ row.total_cases }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140">
          <template #default="{ row }">
            <el-button link type="primary" @click="select(row.eval_id)">查看</el-button>
            <el-button
              link
              type="danger"
              :disabled="row.status === 'running' || authRole !== 'admin'"
              @click="confirmDelete(row)"
            >删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!reports.length && !listError" description="暂无评测记录" :image-size="70" />
    </el-card>

    <!-- 运行中进度 -->
    <el-card v-if="running && report" shadow="never" class="eval-progress-card">
      <template #header>
        <span class="card-title">评测执行中</span>
        <el-tag type="primary" size="small" effect="plain">每 2 秒自动刷新</el-tag>
      </template>
      <el-progress
        :percentage="Math.round((report.completed_cases / report.total_cases) * 100)"
        :status="'active'"
        :stroke-width="14"
        :text-inside="true"
      />
      <p class="hint progress-tip">
        已完成 {{ report.completed_cases }} / {{ report.total_cases }} 个用例
        （{{ report.eval_id }}；部分进度与结果持续保存）
      </p>
    </el-card>

    <el-empty v-if="!report && !running" description="选择左侧历史评测查看结果" />

    <!-- 评测结果 -->
    <template v-if="report && !running">
      <el-alert
        v-if="report.status === 'failed'"
        :title="`评测中断：${report.error || '未知错误'}`"
        type="error"
        show-icon
        :closable="false"
        class="eval-alert"
      />

      <!-- 五维指标卡 -->
      <el-row :gutter="16" class="metric-row">
        <el-col v-for="m in metricCards" :key="m.key" :span="4">
          <el-card shadow="hover" class="metric-card">
            <div class="metric-label">{{ m.label }}</div>
            <div class="metric-value" :class="{ 'metric-bad': !m.met }">
              {{ m.value }}
            </div>
            <div class="metric-target">
              目标 {{ m.target }}
              <el-tag :type="m.met ? 'success' : 'danger'" size="small" effect="plain">
                {{ m.met ? '达标' : '未达标' }}
              </el-tag>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="16">
        <!-- 雷达图 -->
        <el-col :span="10">
          <el-card shadow="never">
            <template #header>
              <span class="card-title">五维指标雷达图</span>
              <el-tag size="small" type="info" effect="plain">归一化 0~100</el-tag>
            </template>
            <div ref="radarEl" class="radar-chart" />
          </el-card>
        </el-col>

        <!-- Markdown 报告 -->
        <el-col :span="14">
          <el-card shadow="never">
            <template #header>
              <div class="md-header">
                <span class="card-title">Markdown 评测报告</span>
                <el-switch v-model="showMd" size="small" active-text="显示" inactive-text="隐藏" />
              </div>
            </template>
            <div v-if="showMd" class="md-content" v-html="reportMdHtml" />
          </el-card>
        </el-col>
      </el-row>

      <!-- 用例明细 -->
      <el-card shadow="never" class="case-card">
        <template #header>
          <div class="md-header">
            <span class="card-title">用例明细（{{ summary?.success_cases }}/{{ summary?.total_cases }} {{ (summary?.metric_version ?? 0) >= 2 ? '质量通过' : '旧版执行成功' }}）</span>
            <el-switch v-model="showCases" size="small" active-text="显示" inactive-text="隐藏" />
          </div>
        </template>
        <el-table v-if="showCases" :data="report.results" size="small" border>
          <el-table-column prop="case_index" label="#" width="50" />
          <el-table-column prop="topic" label="主题" min-width="180" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="caseStatusType(row)" size="small">{{ caseStatusLabel(row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="90">
            <template #default="{ row }">
              <el-tag :type="durationType(row)" size="small" effect="plain">{{ durationText(row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="react_loops" label="ReAct 次数" width="90" />
          <el-table-column label="关键词覆盖率" width="110">
            <template #default="{ row }">
              {{ coveragePercent(row.keyword_coverage) }}
            </template>
          </el-table-column>
          <el-table-column prop="memory_hits" label="记忆命中" width="90" />
          <el-table-column label="强制输出" width="90">
            <template #default="{ row }">
              <el-tag v-if="row.forced_pass" type="warning" size="small">⚠️ 是</el-tag>
              <span v-else class="hint">—</span>
            </template>
          </el-table-column>
          <el-table-column prop="task_id" label="任务 ID" min-width="130">
            <template #default="{ row }">
              <code>{{ row.task_id }}</code>
            </template>
          </el-table-column>
          <el-table-column label="异常" min-width="140">
            <template #default="{ row }">
              <span v-if="row.error" class="error-text">{{ row.error }}</span>
              <span v-else class="hint">—</span>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.evaluation-page {
  max-width: 1200px;
  margin: 0 auto;
}
.eval-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.eval-header h2 {
  margin: 0 0 8px;
  font-size: 20px;
}
.hint {
  color: #909399;
  font-size: 13px;
  line-height: 20px;
  margin: 0;
}
.card-title {
  font-weight: 600;
  font-size: 14px;
  margin-right: 8px;
}
.eval-list-card,
.eval-progress-card,
.eval-alert,
.metric-row,
.case-card {
  margin-bottom: 16px;
}
.progress-tip {
  margin-top: 10px;
}
.metric-card {
  text-align: center;
}
.metric-label {
  color: #909399;
  font-size: 12px;
  margin-bottom: 6px;
}
.metric-value {
  font-size: 24px;
  font-weight: 700;
  color: #303133;
}
.metric-bad {
  color: #f56c6c;
}
.metric-target {
  margin-top: 6px;
  font-size: 12px;
  color: #909399;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
}
.radar-chart {
  height: 320px;
}
.md-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.md-content {
  max-height: 320px;
  overflow: auto;
  font-size: 13px;
  line-height: 1.7;
  color: #303133;
  word-break: break-word;
}
.md-content h1 {
  font-size: 18px;
}
.md-content h2 {
  font-size: 16px;
}
.md-content table {
  border-collapse: collapse;
  margin: 8px 0;
  font-size: 12px;
}
.md-content th,
.md-content td {
  border: 1px solid #e4e7ed;
  padding: 5px 8px;
}
.error-text {
  color: #f56c6c;
  font-size: 12px;
}
</style>
