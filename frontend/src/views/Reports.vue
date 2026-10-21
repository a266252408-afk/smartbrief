<script setup lang="ts">
/** 报告中心：已生成报告的列表页 —— 查看（跳转报告展示页）/ 删除 */
import QualityBadge from '../components/QualityBadge.vue'
import DemoBadge from '../components/DemoBadge.vue'
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { deleteTask, fetchTasks, type TaskSummary } from '../api/tasks'

const router = useRouter()

const loading = ref(false)
const reports = ref<TaskSummary[]>([])
const total = ref(0)
const keyword = ref('')          // 主题搜索框
const query = reactive({
  page: 1,
  page_size: 10,
  order: 'desc' as 'desc' | 'asc'   // 时间排序：desc 最新在前 / asc 最久在前
})

async function load() {
  loading.value = true
  try {
    // 报告中心数据源 = 所有执行完成的任务，独立展示质量
    const result = await fetchTasks({
      page: query.page,
      page_size: query.page_size,
      status: 'completed',
      keyword: keyword.value.trim() || undefined,
      order: query.order
    })
    reports.value = result.items
    total.value = result.total
  } catch {
    ElMessage.error('获取报告列表失败，请检查后端服务')
  } finally {
    loading.value = false
  }
}

function onSearch() {
  query.page = 1   // 搜索后回到第一页
  void load()
}

function onOrderChange() {
  query.page = 1
  void load()
}

function onPageChange(page: number) {
  query.page = page
  void load()
}

/** 报告耗时估算：created_at → updated_at（SQLite 本地时间格式 %Y-%m-%d %H:%M:%S） */
function durationText(row: TaskSummary): string {
  const fmt = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$/
  const start = row.created_at?.match(fmt)
  const end = row.updated_at?.match(fmt)
  if (!start || !end) return '—'
  const secs = Math.max(0, (new Date(end[0].replace(' ', 'T')).getTime() - new Date(start[0].replace(' ', 'T')).getTime()) / 1000)
  if (secs < 60) return `${Math.round(secs)} 秒`
  const m = Math.floor(secs / 60)
  const s = Math.round(secs % 60)
  return `${m} 分 ${s} 秒`
}

/** 查看报告：进入报告展示页（/report/:taskId） */
function openReport(row: TaskSummary) {
  router.push(`/report/${row.task_id}`)
}

/** 删除报告（级联删除其 ReAct 日志，带确认弹窗） */
async function confirmDelete(row: TaskSummary) {
  try {
    await ElMessageBox.confirm(
      `确定删除报告「${row.topic}」（${row.task_id}）吗？其 ReAct 日志将一并删除，该操作不可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await deleteTask(row.task_id)
    ElMessage.success(`已删除报告 ${row.task_id}`)
    // 当前页删空且非第一页时回退一页，避免停留在空页
    if (reports.value.length === 1 && query.page > 1) query.page -= 1
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '删除失败，请检查后端服务')
  }
}

onMounted(load)
</script>

<template>
  <div class="reports-page">
    <div class="reports-header">
      <div>
        <h2>报告中心</h2>
        <p class="hint">全部已生成的市场调研简报（质量状态单独标注），支持搜索、排序、查看与删除</p>
      </div>
      <div class="header-actions">
        <!-- 按主题搜索：回车或点击搜索触发，清空后自动恢复全量 -->
        <el-input
          v-model="keyword"
          class="search-input"
          placeholder="按主题搜索，如：储能 / 智能手表…"
          clearable
          @keydown.enter.prevent="onSearch"
          @clear="onSearch"
        >
          <template #append>
            <el-button @click="onSearch">搜索</el-button>
          </template>
        </el-input>
        <!-- 按时间排序：最新在前 / 最久在前 -->
        <el-select v-model="query.order" class="order-select" @change="onOrderChange">
          <el-option label="最新在前" value="desc" />
          <el-option label="最久在前" value="asc" />
        </el-select>
        <el-button :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" @click="router.push('/home')">新建任务</el-button>
      </div>
    </div>

    <el-card shadow="never">
      <el-table v-loading="loading" :data="reports" size="small" border @row-dblclick="openReport">
        <el-table-column prop="topic" label="报告主题" min-width="220">
          <template #default="{ row }">
            <span class="topic-cell">{{ row.topic }}</span>
            <DemoBadge :metadata="row.metadata" />
          </template>
        </el-table-column>
        <el-table-column prop="task_id" label="任务 ID" width="150">
          <template #default="{ row }">
            <code>{{ row.task_id }}</code>
          </template>
        </el-table-column>
        <el-table-column label="报告质量" min-width="170">
          <template #default="{ row }"><QualityBadge :quality="row.quality_status" :forced="row.forced_output" /></template>
        </el-table-column>
        <el-table-column prop="created_at" label="生成时间" width="180" />
        <el-table-column label="报告耗时" width="120">
          <template #default="{ row }">
            {{ durationText(row) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openReport(row)">查看</el-button>
            <el-button link type="danger" @click="confirmDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !reports.length" description="暂无已生成的报告，先去「任务提交」页生成一份吧" :image-size="80" />

      <div class="pagination-wrap">
        <el-pagination
          :current-page="query.page"
          :page-size="query.page_size"
          :total="total"
          layout="total, prev, pager, next"
          @current-change="onPageChange"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.reports-page {
  max-width: 1100px;
  margin: 0 auto;
}
.reports-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.reports-header h2 {
  margin: 0 0 8px;
  font-size: 20px;
}
.hint {
  color: #909399;
  font-size: 13px;
  margin: 0;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.search-input {
  width: 260px;
}
.order-select {
  width: 130px;
}
.topic-cell {
  font-weight: 500;
}
.pagination-wrap {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>