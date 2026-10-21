<script setup lang="ts">
import RetrievalPanel from '../components/RetrievalPanel.vue'
/** 报告展示页：Markdown 渲染 + 图表图片 + 执行摘要 */
import QualityBadge from '../components/QualityBadge.vue'
import DemoBadge from '../components/DemoBadge.vue'
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { renderReport, escapeHtml } from '../utils/report'
import '../styles/report.css'
import html2pdf from 'html2pdf.js'
import { fetchTaskReport, type TaskReport } from '../api/tasks'

const route = useRoute()
const router = useRouter()
const taskId = route.params.taskId as string

const report = ref<TaskReport | null>(null)
const loading = ref(false)
const notFound = ref(false)
const reportEl = ref<HTMLElement>()

async function load() {
  loading.value = true
  try {
    report.value = await fetchTaskReport(taskId)
    notFound.value = false
  } catch (e: any) {
    if (e?.response?.status === 404) {
      notFound.value = true
    } else {
      ElMessage.error('获取报告失败，请检查后端服务')
    }
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** Markdown → 安全 HTML（DOMPurify 过滤，防 XSS） */
const reportHtml = computed(() => {
  const md = report.value?.content_markdown ?? ''
  if (!md) return ''
  return renderReport(md, report.value?.sources, taskId, report.value?.citations, report.value?.quality_status, {
    generatorVersion: report.value?.generator_version ?? report.value?.metadata?.generator_version,
    demo: report.value?.metadata?.demo === true
  })
})

/** 执行摘要卡（耗时可能为空 → 显示 —） */
const summaryItems = computed(() => {
  const s = report.value?.executive_summary
  if (!s) return []
  return [
    { label: '执行总耗时', value: s.total_time == null ? '—' : `${s.total_time}s` },
    { label: 'ReAct 行动次数', value: `${s.react_loops} 次` },
    { label: '长期记忆命中', value: `${s.memory_hit_count} 条` }
  ]
})

function formatTime(seconds: number | null) {
  if (seconds == null) return '—'
  if (seconds < 60) return `${seconds} 秒`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m} 分 ${s} 秒`
}

/** 导出 PDF：基于渲染后的报告 DOM 生成 */
async function exportPdf() {
  if (!reportEl.value) return
  try {
    await html2pdf()
      .set({
        margin: 10,
        filename: `${report.value?.title ?? '报告'}.pdf`,
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

/** 导出 Word：生成 .doc（HTML 兼容格式） */
function exportWord() {
  const html = `<!DOCTYPE html>
<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word">
<head><meta charset="utf-8"><title>${escapeHtml(report.value?.title ?? '报告')}</title></head>
<body>${reportHtml.value}</body></html>`
  const blob = new Blob(['\ufeff', html], { type: 'application/msword' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${report.value?.title ?? '报告'}.doc`
  a.click()
  URL.revokeObjectURL(url)
  ElMessage.success('Word 文档已导出')
}
</script>

<template>
  <div v-loading="loading" class="report-page">
    <div class="report-header">
      <div>
        <el-button link @click="router.push('/reports')">← 返回报告中心</el-button>
        <h2 class="report-title">{{ report?.title || '报告展示' }}</h2>
        <p class="hint"><code>{{ taskId }}</code>
          <QualityBadge v-if="report" :quality="report.quality_status" :forced="report.forced_output" />
          <DemoBadge :metadata="report?.metadata" />
        </p>
      </div>
      <div v-if="report" class="report-actions">
        <el-button size="small" @click="exportWord">导出 Word</el-button>
        <el-button size="small" type="primary" @click="exportPdf">导出 PDF</el-button>
      </div>
    </div>

    <el-empty v-if="notFound" description="报告不存在或任务尚未完成" />

    <el-alert
      v-if="report?.metadata?.demo"
      data-testid="demo-notice"
      title="演示数据：不代表在线生成成绩"
      description="该报告来自显式导入的演示样例；来源、质量与用量状态仅用于展示页面流程。"
      type="warning"
      show-icon
      :closable="false"
      class="demo-alert"
    />

    <template v-if="report">
      <!-- 执行摘要卡 -->
      <el-row :gutter="16" class="summary-row">
        <el-col v-for="item in summaryItems" :key="item.label" :span="8">
          <el-card shadow="hover" class="summary-card">
            <div class="summary-label">{{ item.label }}</div>
            <div class="summary-value">{{ item.value }}</div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 图表图片（Analyst 图表 Agent 属后续扩展，当前为空） -->
      <el-card v-if="report.charts.length" shadow="never" class="chart-card">
        <template #header>
          <span class="card-title">图表</span>
        </template>
        <div v-for="chart in report.charts" :key="chart.title" class="chart-item">
          <h3>{{ chart.title }}</h3>
          <img :src="chart.image_base64" alt="" />
        </div>
      </el-card>

    <RetrievalPanel v-if="report" :records="report.retrievals" :document-ids="report.document_ids" />

      <!-- Markdown 报告 -->
      <el-card shadow="never" class="report-card">
        <template #header>
          <span class="card-title">调研简报</span>
          <el-tag size="small" type="warning" effect="plain">
            执行耗时 {{ formatTime(report.executive_summary.total_time) }}
          </el-tag>
        </template>
        <div ref="reportEl" class="report-content" v-html="reportHtml" />
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.report-page {
  max-width: 960px;
  margin: 0 auto;
}
.report-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.report-title {
  margin: 12px 0 8px;
  font-size: 20px;
}
.hint {
  color: #909399;
  font-size: 13px;
}
.report-actions {
  display: flex;
  gap: 8px;
}
.demo-alert {
  margin-bottom: 16px;
}
.summary-row {
  margin-bottom: 16px;
}
.summary-card {
  text-align: center;
}
.summary-label {
  color: #909399;
  font-size: 12px;
  margin-bottom: 6px;
}
.summary-value {
  font-size: 20px;
  font-weight: 700;
  color: #303133;
}
.chart-card {
  margin-bottom: 16px;
}
.chart-item {
  margin-bottom: 12px;
}
.chart-item img {
  max-width: 100%;
}
.card-title {
  font-weight: 600;
  font-size: 14px;
  margin-right: 8px;
}
.report-card {
  margin-bottom: 24px;
}
.report-content {
  margin: 0;
  word-break: break-word;
  font-size: 14px;
  line-height: 1.8;
  color: #303133;
}
/* Markdown 渲染后的富文本排版（与任务详情页一致） */
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