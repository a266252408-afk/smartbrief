<script setup lang="ts">
import type { ExecutionStatus } from '../api/tasks'
import { cancelTask, resumeTask } from '../api/tasks'
import { authRole } from '../api/session'
import { onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps<{ taskId: string; status: ExecutionStatus; offline?: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const busy = ref(false)
const notice = ref('')
const error = ref('')
let generation = 0
async function act(action: 'cancel' | 'resume') {
  if (busy.value || authRole.value !== 'admin') return
  if (action === 'resume' && props.offline) return
  const id = props.taskId, version = ++generation
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    if (action === 'cancel') {
      await cancelTask(id)
      if (version !== generation) return
      notice.value = '任务已取消'
    } else {
      const result = await resumeTask(id)
      if (version !== generation) return
      notice.value = !result.resumed ? '任务已在排队或执行' : result.resume_mode === 'checkpoint' ? '已排队，断点续跑' : '已排队，从头重试'
    }
    emit('changed')
  } catch (e: any) {
    if (version === generation) error.value = e?.response?.data?.error?.message || '操作失败，请重试'
  } finally {
    if (version === generation) busy.value = false
  }
}
watch(() => props.taskId, () => { generation++; busy.value = false; notice.value = ''; error.value = '' })
watch(() => props.status, () => { notice.value = '' })
onBeforeUnmount(() => { generation++ })
</script>
<template>
  <span class="task-controls" @click.stop>
    <button v-if="['queued', 'running'].includes(status)" class="cancel" :disabled="busy || authRole !== 'admin'" @click="act('cancel')">{{ busy ? '处理中…' : '取消任务' }}</button>
    <button v-if="!offline && ['paused', 'failed'].includes(status)" :disabled="busy || authRole !== 'admin'" @click="act('resume')">{{ busy ? '处理中…' : '恢复任务' }}</button>
    <span v-if="offline" role="note">离线协议任务，请在评测中心重新运行</span>
    <span v-if="notice" role="status">{{ notice }}</span>
    <span v-if="error" role="alert">{{ error }}</span>
  </span>
</template>
<style scoped>
.task-controls { display: inline-flex; gap: 8px; align-items: center; flex-wrap: wrap; }
button { border: 1px solid #c8d8f1; background: white; color: #245bc2; padding: 7px 12px; border-radius: 4px; cursor: pointer; white-space: nowrap; }
button.cancel { color: #b42318; border-color: #f5bcb8; }
button:disabled { opacity: .5; cursor: not-allowed; }
[role="status"] { color: #54667c; font-size: 13px; }
[role="alert"] { color: #b42318; font-size: 13px; }
</style>
