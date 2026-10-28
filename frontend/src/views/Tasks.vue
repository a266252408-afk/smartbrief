<script setup lang="ts">
import TaskControls from '../components/TaskControls.vue'
import { isOfflineTask } from '../utils/taskOrigin'
import { onMounted, onBeforeUnmount, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { deleteTask, fetchTasks, type TaskSummary, type ExecutionStatus } from '../api/tasks'

const router = useRouter()

const loading = ref(false)
const tasks = ref<TaskSummary[]>([])
const total = ref(0)
const query = reactive({
  page: 1,
  page_size: 10,
  status: '' as '' | ExecutionStatus
})

const statusMeta: Record<string, { label: string; type: 'primary' | 'success' | 'danger' | 'warning' | 'info' }> = {
  queued: { label: '排队中', type: 'warning' },
  cancelled: { label: '已取消', type: 'info' },
  running: { label: '执行中', type: 'primary' },
  paused: { label: '已暂停', type: 'warning' },
  completed: { label: '已完成', type: 'success' },
  failed: { label: '失败', type: 'danger' }
}

function statusOf(status: string) {
  return statusMeta[status] ?? { label: status, type: 'info' as const }
}

let generation = 0
let timer: ReturnType<typeof setInterval> | null = null
async function load() {
  const version = ++generation
  loading.value = true
  try {
    const result = await fetchTasks({
      page: query.page,
      page_size: query.page_size,
      status: query.status || undefined
    })
    if (version !== generation) return
    tasks.value = result.items
    total.value = result.total
  } catch {
    if (version !== generation) return
    ElMessage.error('获取任务列表失败，请检查后端服务')
  } finally {
    if (version === generation) loading.value = false
  }
}

function onStatusChange(status: '' | ExecutionStatus) {
  query.status = status
  query.page = 1
  void load()
}

function openDetail(task: TaskSummary) {
  router.push(`/tasks/${task.task_id}`)
}

async function confirmDelete(task: TaskSummary) {
  try {
    await ElMessageBox.confirm(
      `确定删除任务「${task.topic}」（${task.task_id}）吗？其 ReAct 日志将一并删除，该操作不可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await deleteTask(task.task_id)
    ElMessage.success(`已删除任务 ${task.task_id}`)
    // 当前页删空且非第一页时回退一页，避免停留在空页
    if (tasks.value.length === 1 && query.page > 1) query.page -= 1
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '删除失败，请检查后端服务')
  }
}

onMounted(() => {
  void load()
  timer = setInterval(() => {
    if (!loading.value && tasks.value.some(task => ['queued', 'running'].includes(task.status))) void load()
  }, 2000)
})
onBeforeUnmount(() => { generation++; if (timer) clearInterval(timer) })
</script>

<template>
  <div>
    <div class="tasks-header">
      <div>
        <h2>任务列表</h2>
        <p class="hint">查看报告生成任务的进度、执行记录与结果</p>
      </div>
      <div class="header-actions">
        <el-button :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" @click="router.push('/home')">新建任务</el-button>
      </div>
    </div>

    <el-tabs :model-value="query.status" @tab-change="onStatusChange">
      <el-tab-pane label="全部" name="" />
      <el-tab-pane label="排队中" name="queued" />
      <el-tab-pane label="已取消" name="cancelled" />
      <el-tab-pane label="执行中" name="running" />
      <el-tab-pane label="已暂停" name="paused" />
      <el-tab-pane label="已完成" name="completed" />
      <el-tab-pane label="失败" name="failed" />
    </el-tabs>

    <el-table v-loading="loading" :data="tasks" @row-click="openDetail" class="task-table">
      <el-table-column prop="task_id" label="任务 ID" width="160">
        <template #default="{ row }">
          <code class="task-id">{{ row.task_id }}</code>
        </template>
      </el-table-column>
      <el-table-column prop="topic" label="主题" min-width="220" show-overflow-tooltip />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="statusOf(row.status).type" size="small">{{ statusOf(row.status).label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="步骤进度" width="120">
        <template #default="{ row }">
          <span v-if="row.plan.length" class="step-progress">
            {{ row.completed_steps.length }}/{{ row.plan.length }}
          </span>
          <span v-else class="hint">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="提交时间" width="170" />
      <el-table-column label="操作" min-width="280">
        <template #default="{ row }">
          <el-button link type="primary" @click.stop="openDetail(row)">查看</el-button>
          <el-button link type="danger" @click.stop="confirmDelete(row)">删除</el-button>
          <TaskControls :task-id="row.task_id" :status="row.status" :offline="isOfflineTask(row)" @changed="load" />
        </template>
      </el-table-column>
    </el-table>

    <div v-if="total > 0" class="pagination">
      <el-pagination
        v-model:current-page="query.page"
        :page-size="query.page_size"
        :total="total"
        layout="prev, pager, next, total"
        @current-change="load"
      />
    </div>
    <el-empty v-if="!loading && total === 0" description="暂无任务，可前往「新建任务」提交一个研究主题" />
  </div>
</template>

<style scoped>
.tasks-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 4px;
}
.tasks-header h2 {
  margin: 0;
  font-size: 20px;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.hint {
  color: #909399;
  font-size: 13px;
}
.task-table {
  cursor: pointer;
}
.task-id {
  font-size: 12px;
  color: #606266;
}
.step-progress {
  font-size: 13px;
  color: #303133;
}
.pagination {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>
