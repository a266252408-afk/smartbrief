<script setup lang="ts">
/** 评测中心：运行评测 → 进度条 → 指标雷达图 + 指标卡 + 用例明细 + 历史对比 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import type { ECharts } from 'echarts'
import EvaluationProtocol from '../components/EvaluationProtocol.vue'
import { authRole } from '../api/session'
import {
  buildMetricCards,
  buildRadarView,
  executionRate,
  formatPercent,
  qualityRate
} from '../utils/evaluationView'
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
      `确定删除评测报告 ${row.eval_id}（已处理 ${row.completed_cases}/${row.total_cases}）吗？该操作不可恢复。`,
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

/** 指标展示卡（实测值 + 目标值 + 达标标签；无目标项只展示） */
const metricCards = computed(() => {
  const metrics = report.value?.metrics
  return metrics?.summary ? buildMetricCards(metrics) : []
})

/** 雷达图数据：目标值=100，达到或优于目标封顶 100。 */
const radarOption = computed(() => {
  const metrics = report.value?.metrics
  if (!metrics?.summary) return null
  const view = buildRadarView(metrics)
  return {
    tooltip: {},
    radar: {
      indicator: view.labels.map((name) => ({ name, max: 100 })),
      radius: '65%'
    },
    series: [
      {
        type: 'radar',
        data: [
          {
            value: view.values,
            name: '指标得分',
            areaStyle: { opacity: 0.25 }
          }
        ]
      }
    ]
  }
})

const radarChart = shallowRef<ECharts | null>(null)
const radarEl = ref<HTMLElement>()
let chartRenderVersion = 0

watch([radarOption, radarEl], async () => {
  if (disposed) return
  if (!radarOption.value || !radarEl.value) {
    chartRenderVersion += 1
    radarChart.value?.dispose()
    radarChart.value = null
    return
  }
  if (radarChart.value && radarChart.value.getDom() !== radarEl.value) {
    radarChart.value.dispose()
    radarChart.value = null
  }
  // ECharts 只在确实有图表数据时加载，不进入评测路由的静态包。
  const version = ++chartRenderVersion
  const element = radarEl.value
  const option = radarOption.value
  const echarts = await import('echarts')
  if (disposed || version !== chartRenderVersion || radarEl.value !== element) return
  if (radarChart.value && radarChart.value.getDom() !== element) {
    radarChart.value.dispose()
    radarChart.value = null
  }
  radarChart.value ??= echarts.init(element)
  radarChart.value.setOption(option)
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
  if (r.quality_passed == null) return 'info' as const
  return 'danger' as const
}

function caseStatusLabel(r: EvalCaseResult) {
  if (r.quality_passed == null) return r.success ? '旧版执行完成（质量未知）' : '旧版执行失败'
  if (r.quality_passed && !r.forced_output) return '质量通过'
  return r.execution_success ? '执行完成，质量未通过' : '执行失败'
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
          protocol-1 固定20例合成协议测试：16 例非故障用例统计质量通过率，4 例故障用例统计处理正确率；
          另附执行完成率、耗时、行动次数、关键词覆盖率与记忆命中率。
          离线结果只验证软件协议，不代表在线效果；在线实验需另行授权，人工事实抽查 pending。
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
        <el-table-column label="已处理" width="90">
          <template #default="{ row }">
            <span>{{ row.completed_cases }} / {{ row.total_cases }}</span>
          </template>
        </el-table-column>
        <el-table-column width="100">
          <template #header>
            <span title="执行状态为 completed 的用例 / 全部用例；与“已处理”不同">执行完成</span>
          </template>
          <template #default="{ row }">
            {{ row.status === 'running' ? '—' : formatPercent(executionRate(row)) }}
          </template>
        </el-table-column>
        <el-table-column width="100">
          <template #header>
            <span title="v3 为非故障用例质量通过率；旧版为执行成功率">质量通过</span>
          </template>
          <template #default="{ row }">
            {{ row.status === 'running' ? '—' : formatPercent(qualityRate(row)) }}
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
        已处理 {{ report.completed_cases }} / {{ report.total_cases }} 个用例
        （其中执行完成 {{ report.metrics?.summary?.execution_success_cases ?? 0 }} 个；{{ report.eval_id }}；部分进度与结果持续保存）
      </p>
    </el-card>

    <el-empty v-if="!report && !running" description="从上方历史评测中选择一条查看结果" />

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

      <!-- 指标卡 -->
      <el-row :gutter="16" class="metric-row">
        <el-col v-for="m in metricCards" :key="m.key" :span="6">
          <el-card shadow="hover" class="metric-card">
            <div class="metric-label">{{ m.label }}</div>
            <div class="metric-value" :class="{ 'metric-bad': m.met === false }">
              {{ m.value }}
            </div>
            <div class="metric-target">
              <template v-if="m.target !== null">
                目标 {{ m.target }}
                <el-tag v-if="m.met !== null" :type="m.met ? 'success' : 'danger'" size="small" effect="plain">
                  {{ m.met ? '达标' : '未达标' }}
                </el-tag>
                <el-tag v-else type="info" size="small" effect="plain">仅展示</el-tag>
              </template>
              <span v-else>仅展示，不参与达标判定</span>
            </div>
            <div v-if="m.detail" class="metric-detail">{{ m.detail }}</div>
          </el-card>
        </el-col>
      </el-row>
      <p class="hint metric-note">
        “正常用例质量通过率”只统计非故障用例（16 例）；“故障处理正确率”考核 4 例故障是否被正确识别且未冒充质量通过。
        整体质量通过率含故障用例，只作为参考，不再套用 90% 目标。
      </p>

      <el-row :gutter="16">
        <!-- 雷达图 -->
        <el-col :span="10">
          <el-card shadow="never">
            <template #header>
              <span class="card-title">指标雷达图</span>
              <el-tag size="small" type="info" effect="plain">目标值 = 100</el-tag>
            </template>
            <div ref="radarEl" class="radar-chart" />
            <p class="hint radar-note">
              雷达图只汇总五项核心维度，完整指标以上方卡片为准。
              耗时/循环以目标值为 100 分：达到目标即 100，超出目标按比例扣分；
              质量维度使用“正常用例质量通过率”。
            </p>
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
            <span class="card-title">
              用例明细（{{ summary?.success_cases }}/{{ summary?.total_cases }}
              {{ (summary?.metric_version ?? 0) >= 3 ? '整体质量通过，含故障用例，仅供参考' : (summary?.metric_version ?? 0) >= 2 ? '质量通过' : '旧版执行成功' }}）
            </span>
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
              {{ formatPercent(row.keyword_coverage) }}
            </template>
          </el-table-column>
          <el-table-column prop="memory_hits" label="记忆命中" width="90" />
          <el-table-column label="强制输出" width="90">
            <template #default="{ row }">
              <el-tag v-if="row.forced_output ?? row.forced_pass" type="warning" size="small">⚠️ 是</el-tag>
              <span v-else class="hint">—</span>
            </template>
          </el-table-column>
          <el-table-column label="失败分类" min-width="140">
            <template #default="{ row }">
              <code v-if="row.failure_category">{{ row.failure_category }}</code>
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
.metric-detail {
  margin-top: 4px;
  font-size: 11px;
  color: #a8abb2;
  line-height: 16px;
}
.metric-note {
  margin: -4px 0 16px;
}
.radar-chart {
  height: 320px;
}
.radar-note {
  margin-top: 6px;
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
