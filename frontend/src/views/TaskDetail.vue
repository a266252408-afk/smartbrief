<script setup lang="ts">
import QualityBadge from '../components/QualityBadge.vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { renderReport, escapeHtml } from '../utils/report'
import '../styles/report.css'
import html2pdf from 'html2pdf.js'
import { fetchTaskStatus, resumeTask, type TaskStatus, type ReactLog, type ReviewRound } from '../api/tasks'

const route = useRoute()
const router = useRouter()

const taskId = route.params.taskId as string

const detail = ref<TaskStatus | null>(null)
const loading = ref(false)
const notFound = ref(false)
const pollTimer = ref<number | null>(null)

// ---------------- 轮询 ----------------

async function refresh() {
  loading.value = true
  try {
    detail.value = await fetchTaskStatus(taskId)
    notFound.value = false
  } catch (e: any) {
    if (e?.response?.status === 404) {
      notFound.value = true
      stopPolling()
    } else {
      ElMessage.error('获取任务状态失败')
    }
  } finally {
    loading.value = false
  }
}

function stopPolling() {
  if (pollTimer.value !== null) {
    window.clearInterval(pollTimer.value)
    pollTimer.value = null
  }
}

function startPolling() {
  stopPolling()
  pollTimer.value = window.setInterval(() => {
    if (detail.value?.status === 'running') {
      void refresh()
    } else {
      stopPolling()
    }
  }, 2000)
}

onMounted(async () => {
  await refresh()
  startPolling()
})

onBeforeUnmount(stopPolling)

// ---------------- 派生数据 ----------------

const isRunning = computed(() => detail.value?.status === 'running')

/** 状态标签（含paused：服务中断后由后端启动扫描自动标记） */
const statusInfo = computed<{ label: string; type: 'primary' | 'warning' | 'success' | 'danger' | 'info' }>(() => {
  const s = detail.value?.status
  if (s === 'running') return { label: '执行中', type: 'primary' }
  if (s === 'paused') return { label: '已暂停', type: 'warning' }
  if (s === 'completed') return { label: '执行完成', type: 'info' }
  return { label: '失败', type: 'danger' }
})

const planSteps = computed(() => detail.value?.plan ?? [])

function stepState(stepId: number): 'done' | 'running' | 'pending' {
  if (!detail.value) return 'pending'
  const completed = detail.value.completed_steps ?? []
  if (completed.includes(stepId)) return 'done'
  // 第一个未完成步骤视为当前正在执行
  const firstPending = planSteps.value.find((s) => !completed.includes(s.id))
  return firstPending && firstPending.id === stepId ? 'running' : 'pending'
}

const stepStateMeta = {
  done: { label: '已完成', type: 'success' as const },
  running: { label: '执行中', type: 'primary' as const },
  pending: { label: '等待中', type: 'info' as const }
}

// ---------------- 日志流：按时间顺序分节展示 ----------------

interface LogSection {
  kind: 'step' | 'planning' | 'review' | 'rewrite'
  stepId?: number
  round?: number
  logs: ReactLog[]
}

/** 审核轮次起点：Reviewer 的 Thought 形如「开始审核草稿（第 N 次审核）」 */
function isReviewStart(log: ReactLog) {
  return log.log_type === 'Thought' && log.content.includes('开始审核草稿')
}

/** 返工起点：Rewriter 的 Thought「开始根据审核意见修改草稿」 */
function isRewriteStart(log: ReactLog) {
  return log.log_type === 'Thought' && log.content.includes('开始根据审核意见修改草稿')
}

function reviewRound(content: string): number {
  const m = content.match(/第\s*(\d+)\s*次审核/)
  return m ? Number(m[1]) : 0
}

/**
 * 把扁平日志流组织成按真实时序的展示分节：
 * - step_id>0 的日志 → 「步骤 N」组（Plan 执行）
 * - step_id=0 的日志 → 按内容锚点切为「规划 / A2A 审核第 N 次 / A2A 改写返工」小节。
 *   Reviewer/Rewriter 的日志 step_id 同为 0（与 Planner 的规划日志混在同一数值），
 *   必须靠内容识别才能真正体现出 review/rewrite 过程，否则只能显示成无意义的「步骤 0」。
 */
const logSections = computed<LogSection[]>(() => {
  const logs = detail.value?.react_logs ?? []
  const sections: LogSection[] = []
  let cur: LogSection | null = null
  for (const log of logs) {
    if (log.step_id > 0) {
      // Plan 步骤日志：同一 step 连续聚合（执行严格按步骤串行，不会交错）
      if (!cur || cur.kind !== 'step' || cur.stepId !== log.step_id) {
        cur = { kind: 'step', stepId: log.step_id, logs: [] }
        sections.push(cur)
      }
    } else if (isReviewStart(log)) {
      // 进入新一轮审核（可能发生在规划之后，也可能在返工之后再次审核）
      cur = { kind: 'review', round: reviewRound(log.content), logs: [] }
      sections.push(cur)
    } else if (isRewriteStart(log)) {
      cur = { kind: 'rewrite', logs: [] }
      sections.push(cur)
    } else {
      // 无锚点的 step0 日志（Planner 的「计划已生成」等规划阶段记录）
      if (!cur || cur.kind === 'step') {
        cur = { kind: 'planning', logs: [] }
        sections.push(cur)
      }
    }
    cur.logs.push(log)
  }
  return sections
})

function sectionKey(sec: LogSection): string {
  if (sec.kind === 'step') return `step-${sec.stepId}`
  if (sec.kind === 'review') return `review-${sec.round}`
  return sec.kind
}

function sectionTitle(sec: LogSection): string {
  if (sec.kind === 'step') return `步骤 ${sec.stepId}`
  if (sec.kind === 'planning') return '规划阶段'
  if (sec.kind === 'review') return `A2A 审核 · 第 ${sec.round} 次`
  return 'A2A 改写返工'
}

function logTypeTag(type: ReactLog['log_type']) {
  if (type === 'Thought') return { label: 'Thought', type: 'warning' as const }
  if (type === 'Action') return { label: 'Action', type: 'primary' as const }
  return { label: 'Observation', type: 'success' as const }
}

// ---------------- A2A 审核循环 ----------------

const reviewComments = computed(() => detail.value?.review_comments ?? [])

/**
 * 各轮审核记录（按轮分组展示，轮次与 ReAct 日志中的「A2A 审核 · 第 N 次」小节一一对应）。
 * 优先用后端 review_history（每轮独立）；旧任务无该字段时回退为“最后一次审核意见”单轮。
 */
const reviewRounds = computed<ReviewRound[]>(() => {
  const history = detail.value?.review_history
  if (history && history.length) return history
  if (reviewComments.value.length)
    return [{ round: detail.value?.iteration ?? 0, passed: false, comments: reviewComments.value }]
  return []
})

/** 审核循环进度文案：第 N 次（上限 M）/ 已达上限 */
const iterationText = computed(() => {
  const it = detail.value?.iteration ?? 0
  const max = detail.value?.max_iterations ?? 3
  return it >= max ? `已达上限 ${it}/${max}` : `第 ${it} 次（上限 ${max}）`
})

/** 审核状态与执行状态独立，旧passed字段不作为质量判据。 */
const showReviewPanel = computed(() => !!detail.value)

// ---------------- 断点续跑 ----------------

const resuming = ref(false)

async function onResume() {
  const status = detail.value?.status
  if (status !== 'paused') {
    return // 恢复入口仅对「已暂停」任务开放；执行中/失败/已完成任务不需要恢复
  }
  try {
    await ElMessageBox.confirm(
      '该任务因服务中断已自动暂停。确认从最近 checkpoint 断点续跑，继续完成剩余步骤吗？',
      '恢复执行',
      { type: 'warning', confirmButtonText: '断点续跑', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  resuming.value = true
  try {
    await resumeTask(taskId)
    ElMessage.success('任务已恢复，正在从最近 checkpoint 断点续跑…')
    startPolling()
    await refresh()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '恢复任务失败，请检查后端服务')
  } finally {
    resuming.value = false
  }
}

// ---------------- 长期记忆命中 ----------------

const memoryHits = computed(() => detail.value?.memory_hits ?? [])

function scorePercent(score: number) {
  const pct = Math.max(0, Math.min(1, score)) * 100
  return `${pct.toFixed(0)}%`
}

function scoreType(score: number) {
  if (score >= 0.7) return 'success' as const
  if (score >= 0.45) return 'warning' as const
  return 'info' as const
}

// ---------------- 报告草稿：markdown 渲染 + 导出 ----------------

const reportEl = ref<HTMLElement>()

/** Markdown → 安全 HTML（DOMPurify 过滤，防止 XSS） */
const reportHtml = computed(() => {
  const md = detail.value?.draft_content ?? ''
  if (!md) return ''
  return renderReport(md, detail.value?.evidence, taskId, detail.value?.citations, detail.value?.quality_status)
})

/** 导出 Word：生成 .doc（HTML 兼容格式，Word 可直接打开） */
function exportWord() {
  const html = `<!DOCTYPE html>
<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word">
<head><meta charset="utf-8"><title>${escapeHtml(detail.value?.topic ?? '报告')}</title></head>
<body>${reportHtml.value}</body></html>`
  const blob = new Blob(['\ufeff', html], { type: 'application/msword' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${detail.value?.topic ?? '报告'}.doc`
  a.click()
  URL.revokeObjectURL(url)
  ElMessage.success('Word 文档已导出')
}

/** 导出 PDF：基于渲染后的报告 DOM 生成 */
async function exportPdf() {
  if (!reportEl.value) return
  try {
    await html2pdf()
      .set({
        margin: 10,
        filename: `${detail.value?.topic ?? '报告'}.pdf`,
        image: { type: 'jpeg', quality: 0.95 },
        html2canvas: { scale: 2, useCORS: true },
        jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' }
      })
      .from(reportEl.value)
      .save()
    ElMessage.success('PDF 已导出')
  } catch {
    ElMessage.error('PDF 导出失败')
  }
}
</script>

<template>
  <div v-loading="loading" class="detail-page">
    <div class="detail-header">
      <div>
        <el-button link @click="router.push('/tasks')">← 返回任务列表</el-button>
        <h2 class="topic">{{ detail?.topic || '任务详情' }}</h2>
        <p class="hint">
          <code>{{ taskId }}</code>
          <el-tag v-if="detail" :type="statusInfo.type" size="small" class="status-tag">
            {{ statusInfo.label }}
          </el-tag>
          <QualityBadge v-if="detail" :quality="detail.quality_status" :forced="detail.forced_output" />
          <span v-if="detail?.error" class="error-text">{{ detail.error }}</span>
        </p>
      </div>
      <div class="header-actions">
        <el-button
          v-if="detail && detail.status === 'paused'"
          type="warning"
          :loading="resuming"
          :disabled="loading"
          @click="onResume"
        >恢复执行</el-button>
        <el-button :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <el-empty v-if="notFound" description="任务不存在或已被删除" />

    <!-- A2A 审核循环：循环次数 + 审核状态 + 意见气泡 + 强制输出警告 -->
    <el-card v-if="detail && showReviewPanel" shadow="never" class="review-card">
      <template #header>
        <div class="review-header">
          <span class="card-title">A2A 审核循环</span>
          <div class="review-tags">
            <el-tag size="small" effect="plain">循环：{{ iterationText }}</el-tag>
            <QualityBadge :quality="detail.quality_status" :forced="detail.forced_output" />
          </div>
        </div>
      </template>
      <el-alert
        v-if="detail.forced_output"
        title="已达循环上限，草稿已强制输出"
        description="部分章节未通过审核，质量仍为未通过，并在草稿末尾追加了警告标记。"
        type="warning"
        show-icon
        :closable="false"
        class="review-alert"
      />
      <div v-if="reviewRounds.length" class="review-rounds">
        <div v-for="round in reviewRounds" :key="round.round" class="review-round">
          <div class="review-round-header">
            <span class="review-bubble-icon">审</span>
            <span class="review-round-title">第 {{ round.round }} 次审核</span>
            <el-tag
              :type="(!detail.quality_status || detail.quality_status === 'unknown') ? 'info' : round.passed === true ? 'success' : round.passed === null ? 'warning' : 'danger'"
              size="small"
              effect="plain"
            >
              {{ (!detail.quality_status || detail.quality_status === 'unknown') ? '历史记录（质量未知）' : round.passed === true ? '通过' : round.passed === null ? '审核不可用' : '未通过' }}
            </el-tag>
          </div>
          <div v-if="round.comments?.length" class="review-bubbles">
            <div v-for="(c, i) in round.comments" :key="i" class="review-bubble">
              <div class="review-bubble-text">{{ c }}</div>
            </div>
          </div>
          <div v-else class="review-empty-tip">无具体意见</div>
        </div>
      </div>
      <el-empty v-else description="暂无审核意见" :image-size="60" />
    </el-card>

    <!-- 长期记忆命中：Planner 规划前参考的相似历史报告 -->
    <el-card v-if="detail && memoryHits.length" shadow="never" class="memory-card">
      <template #header>
        <div class="memory-header">
          <span class="card-title">长期记忆命中</span>
          <div class="review-tags">
            <el-tag type="warning" size="small" effect="plain">
              Planner 参考了 {{ memoryHits.length }} 篇历史报告
            </el-tag>
          </div>
        </div>
      </template>
      <div class="memory-hits">
        <div v-for="(hit, i) in memoryHits" :key="i" class="memory-hit">
          <div class="memory-hit-head">
            <span class="memory-hit-icon">忆</span>
            <span class="memory-hit-topic">{{ hit.topic }}</span>
            <el-tag :type="scoreType(hit.score)" size="small" effect="plain">
              相似度 {{ scorePercent(hit.score) }}
            </el-tag>
            <span v-if="hit.created_at" class="hint">{{ hit.created_at }}</span>
            <code v-if="hit.task_id" class="memory-hit-task">{{ hit.task_id }}</code>
          </div>
          <div v-if="hit.structure?.length" class="memory-hit-structure">
            <el-tag
              v-for="(h, j) in hit.structure"
              :key="j"
              size="small"
              type="info"
              effect="plain"
            >{{ h }}</el-tag>
          </div>
          <pre v-if="hit.snippet" class="memory-hit-snippet">{{ hit.snippet }}</pre>
        </div>
      </div>
    </el-card>

    <el-row v-if="detail" :gutter="16">
      <!-- 左：Plan 步骤看板 -->
      <el-col :span="10">
        <el-card shadow="never">
          <template #header>
            <span class="card-title">执行计划（{{ planSteps.length }} 步）</span>
          </template>
          <el-steps direction="vertical" :active="detail.current_step_index">
            <el-step
              v-for="step in planSteps"
              :key="step.id"
              :title="`步骤 ${step.id} · ${step.agent === 'researcher' ? '搜索' : '撰写'}`"
              :status="stepState(step.id) === 'done' ? 'success' : stepState(step.id) === 'running' ? 'process' : 'wait'"
            >
              <template #description>
                <div class="step-desc">
                  <p>{{ step.task }}</p>
                  <el-tag :type="stepStateMeta[stepState(step.id)].type" size="small">
                    {{ stepStateMeta[stepState(step.id)].label }}
                  </el-tag>
                  <span v-if="step.depends_on?.length" class="deps">依赖：{{ step.depends_on.join(', ') }}</span>
                </div>
              </template>
            </el-step>
            <!-- A2A 审核循环：作为计划链路后的收尾阶段节点 -->
            <el-step
              v-if="detail && showReviewPanel"
              title="A2A 审核循环"
              :status="detail.quality_status === 'passed' ? 'success' : detail.quality_status === 'pending' ? 'process' : 'error'"
            >
              <template #description>
                <div class="step-desc">
                  <p>{{ iterationText }}</p>
                  <QualityBadge :quality="detail.quality_status" :forced="detail.forced_output" />
                </div>
              </template>
            </el-step>
          </el-steps>
        </el-card>
      </el-col>

      <!-- 右：ReAct 日志流 -->
      <el-col :span="14">
        <el-card shadow="never">
          <template #header>
            <span class="card-title">ReAct 推理日志</span>
            <el-tag v-if="isRunning" type="primary" size="small" effect="plain">轮询中…</el-tag>
          </template>
          <div class="log-list">
            <template v-for="sec in logSections" :key="sectionKey(sec)">
              <div class="log-step-title" :class="sec.kind !== 'step' ? 'a2a-section-title' : ''">
                <span
                  v-if="sec.kind === 'review' || sec.kind === 'rewrite'"
                  class="a2a-dot"
                  :class="sec.kind === 'review' ? 'a2a-dot-review' : 'a2a-dot-rewrite'"
                >
                  {{ sec.kind === 'review' ? '审' : '改' }}
                </span>
                {{ sectionTitle(sec) }}
                <el-tag
                  v-if="sec.kind === 'review'"
                  type="warning"
                  size="small"
                  effect="plain"
                  class="a2a-role-tag"
                >
                  Reviewer
                </el-tag>
                <el-tag
                  v-else-if="sec.kind === 'rewrite'"
                  type="danger"
                  size="small"
                  effect="plain"
                  class="a2a-role-tag"
                >
                  Rewriter
                </el-tag>
              </div>
              <div
                v-for="(log, i) in sec.logs"
                :key="i"
                class="log-item"
                :class="`log-${log.log_type.toLowerCase()}`"
              >
                <el-tag :type="logTypeTag(log.log_type).type" size="small" class="log-type">
                  {{ log.log_type }}
                </el-tag>
                <pre class="log-content">{{ log.content }}</pre>
              </div>
            </template>
            <el-empty v-if="logSections.length === 0 && !isRunning" description="暂无推理日志" />
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 报告草稿 -->
    <el-card v-if="detail?.draft_content" shadow="never" class="report-card">
      <template #header>
        <div class="report-header">
          <span class="card-title">报告草稿</span>
          <div class="report-actions">
            <el-button size="small" @click="exportWord">导出 Word</el-button>
            <el-button size="small" type="primary" @click="exportPdf">导出 PDF</el-button>
          </div>
        </div>
      </template>
      <div ref="reportEl" class="report-content" v-html="reportHtml" />
    </el-card>
  </div>
</template>

<style scoped>
.detail-page {
  max-width: 1200px;
  margin: 0 auto;
}
.detail-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.detail-header h2 {
  margin: 12px 0 8px;
  font-size: 20px;
}
.hint {
  color: #909399;
  font-size: 13px;
}
.status-tag {
  margin-left: 8px;
}
.error-text {
  color: #f56c6c;
  font-size: 13px;
  margin-left: 8px;
}
.card-title {
  font-weight: 600;
  font-size: 14px;
  margin-right: 8px;
}
.card-title + .el-tag {
  margin-left: 8px;
}
.step-desc p {
  margin: 0 0 6px;
  color: #606266;
  font-size: 13px;
}
.step-desc .deps {
  margin-left: 8px;
  color: #909399;
  font-size: 12px;
}
.log-list {
  max-height: 560px;
  overflow: auto;
}
.log-step-title {
  font-size: 12px;
  font-weight: 600;
  color: #909399;
  margin: 12px 0 6px;
}
/* A2A 审核/返工小节标题 */
.a2a-section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #606266;
}
.a2a-dot {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  line-height: 18px;
  text-align: center;
  border-radius: 50%;
  color: #fff;
  font-size: 11px;
}
.a2a-dot-review {
  background: #e6a23c;
}
.a2a-dot-rewrite {
  background: #f56c6c;
}
.a2a-role-tag {
  margin-left: 2px;
}
.log-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 8px;
}
.log-type {
  flex-shrink: 0;
  margin-top: 2px;
}
.log-content {
  flex: 1;
  min-width: 0;
  margin: 0;
  padding: 8px;
  border-radius: 4px;
  background: #f5f7fa;
  font-size: 12px;
  color: #303133;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 200px;
  overflow: auto;
}
.log-action .log-content {
  background: #ecf5ff;
}
.log-observation .log-content {
  background: #f0f9eb;
}
/* A2A 审核循环 */
.review-card {
  margin-bottom: 16px;
}
.review-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.review-tags {
  display: flex;
  gap: 8px;
}
.review-alert {
  margin-bottom: 12px;
}
/* 长期记忆命中 */
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.memory-card {
  margin-bottom: 16px;
  border-color: #f3d19e;
}
.memory-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.memory-hits {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.memory-hit {
  border: 1px dashed #e4c692;
  border-radius: 8px;
  padding: 10px 12px;
  background: #fdf6ec;
}
.memory-hit-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.memory-hit-icon {
  flex-shrink: 0;
  width: 20px;
  height: 20px;
  line-height: 20px;
  text-align: center;
  border-radius: 50%;
  background: #e6a23c;
  color: #fff;
  font-size: 12px;
}
.memory-hit-topic {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}
.memory-hit-task {
  font-size: 11px;
  color: #909399;
}
.memory-hit-structure {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 8px;
}
.memory-hit-snippet {
  margin: 8px 0 0;
  padding: 8px;
  background: #fff;
  border-radius: 4px;
  font-size: 12px;
  color: #606266;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 80px;
  overflow: auto;
}
/* A2A 意见按审核轮次分组（与日志「A2A 审核 · 第 N 次」小节对应） */
.review-rounds {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.review-round {
  border: 1px dashed #dcdfe6;
  border-radius: 8px;
  padding: 10px 12px;
  background: #fafafa;
}
.review-round-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.review-round-title {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}
.review-empty-tip {
  font-size: 12px;
  color: #c0c4cc;
}
.review-bubble-title {
  font-size: 13px;
  font-weight: 600;
  color: #606266;
  margin-bottom: 8px;
}
.review-bubble {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 8px;
}
.review-bubble-icon {
  flex-shrink: 0;
  width: 20px;
  height: 20px;
  line-height: 20px;
  text-align: center;
  border-radius: 50%;
  background: #409eff;
  color: #fff;
  font-size: 12px;
}
.review-bubble-text {
  color: #303133;
  font-size: 13px;
  line-height: 1.6;
  word-break: break-word;
}
.report-card {
  margin-top: 16px;
}
.report-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.report-actions {
  display: flex;
  gap: 8px;
}
.report-content {
  margin: 0;
  word-break: break-word;
  font-size: 14px;
  line-height: 1.8;
  color: #303133;
}
/* Markdown 渲染后的富文本排版 */
.report-content h1,
.report-content h2,
.report-content h3 {
  margin: 1em 0 0.5em;
}
.report-content h1 {
  font-size: 22px;
}
.report-content h2 {
  font-size: 18px;
}
.report-content h3 {
  font-size: 16px;
}
.report-content p {
  margin: 0.5em 0;
}
.report-content ul,
.report-content ol {
  padding-left: 1.6em;
  margin: 0.5em 0;
}
.report-content blockquote {
  margin: 0.5em 0;
  padding-left: 12px;
  border-left: 3px solid #e4e7ed;
  color: #606266;
}
.report-content code {
  background: #f5f7fa;
  border-radius: 3px;
  padding: 1px 5px;
  font-size: 13px;
}
.report-content pre {
  background: #f5f7fa;
  border-radius: 6px;
  padding: 12px;
  overflow: auto;
}
.report-content pre code {
  background: none;
  padding: 0;
}
.report-content table {
  border-collapse: collapse;
  margin: 0.5em 0;
}
.report-content th,
.report-content td {
  border: 1px solid #e4e7ed;
  padding: 6px 10px;
}
.report-content a {
  color: #409eff;
}
</style>
