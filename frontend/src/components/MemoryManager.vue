<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { fetchMemories, deleteMemory, type MemoryEntry } from '../api/knowledge'
import { regenerateTask } from '../api/tasks'
import { authRole } from '../api/session'

const router = useRouter()
const visible = ref(false)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const items = ref<MemoryEntry[]>([])
const total = ref(0)
const page = ref(1)
const filter = ref<MemoryEntry['status'] | ''>('')
const enabled = ref(true)
const labels: Record<MemoryEntry['status'], string> = {
  active: '准入通过', unverified: '历史未验证', unavailable: '暂不可用', invalid: '已失效', deleted: '已删除'
}
async function load() {
  loading.value = true
  error.value = ''
  try {
    const result = await fetchMemories(page.value, filter.value || undefined)
    items.value = result.items; total.value = result.total; enabled.value = result.enabled
  } catch {
    items.value = []; total.value = 0
    error.value = '记忆库读取失败，请重试；不能据此认定记忆为空'
  } finally { loading.value = false }
}
async function open() { visible.value = true; page.value = 1; await load() }
async function filtered() { page.value = 1; await load() }
async function remove(entry: MemoryEntry) {
  try { await ElMessageBox.confirm(`删除「${entry.topic}」的记忆？原任务报告会保留。`, '删除记忆', { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }) }
  catch { return }
  busy.value = true; error.value = ''
  try { await deleteMemory(entry.memory_id); await load() }
  catch { error.value = '删除未完成，请重试；已登记删除的记忆不会再召回' }
  finally { busy.value = false }
}
async function regenerate(entry: MemoryEntry) {
  busy.value = true; error.value = ''
  try {
    const task = await regenerateTask(entry.task_id)
    visible.value = false
    await router.push(`/tasks/${task.task_id}`)
  } catch { error.value = '重新生成失败，原报告保持不变；原任务可能已删除或仍在运行' }
  finally { busy.value = false }
}
</script>

<template>
  <el-button @click="open">管理长期记忆</el-button>
  <el-dialog v-model="visible" title="长期记忆管理" width="92%" top="4vh">
    <p>记忆仅供规划章节结构参考，不替代当前证据。历史未验证、失效或已删除记录不会参与可信召回。</p>
    <p v-if="!enabled">记忆开关已关闭，数据仍保留，可继续管理。</p>
    <el-select v-model="filter" aria-label="记忆状态" style="width: 180px" @change="filtered">
      <el-option label="全部状态" value="" />
      <el-option v-for="(label, value) in labels" :key="value" :label="label" :value="value" />
    </el-select>
    <el-button :loading="loading" @click="load">刷新记忆</el-button>
    <p v-if="error" role="alert">{{ error }}</p>
    <el-table v-if="!error || items.length" v-loading="loading" :data="items" class="memory-table">
      <el-table-column prop="topic" label="原报告主题" min-width="180" />
      <el-table-column label="状态" width="130"><template #default="{ row }">{{ labels[row.status as MemoryEntry['status']] }}</template></el-table-column>
      <el-table-column prop="task_id" label="原任务" min-width="150" />
      <el-table-column label="模型配置与提示版本" min-width="210"><template #default="{ row }">{{ row.model_name || '模型未知' }} · 供应商版本 {{ row.model_version || '未知' }} · {{ row.prompt_version || '提示版本未知' }}</template></el-table-column>
      <el-table-column label="时间与失效说明" min-width="180"><template #default="{ row }">{{ row.created_at || '时间未知' }}<br>{{ row.invalid_reason || '—' }}</template></el-table-column>
      <el-table-column label="操作" width="180"><template #default="{ row }">
        <el-button link :disabled="busy || authRole === 'readonly' || row.status === 'deleted'" @click="remove(row)">删除记忆</el-button>
        <el-button link :disabled="busy || authRole === 'readonly' || !row.task_id" @click="regenerate(row)">重新生成</el-button>
      </template></el-table-column>
    </el-table>
    <el-pagination v-if="!error" v-model:current-page="page" :page-size="10" :total="total" layout="prev, pager, next, total" @current-change="load" />
  </el-dialog>
</template>
