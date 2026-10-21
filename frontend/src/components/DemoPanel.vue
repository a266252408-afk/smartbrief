<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { authRole } from '../api/session'
import {
  fetchDemoEnvironment,
  fetchDemoSamples,
  importDemoSamples,
  type DemoEnvironment,
  type DemoSample,
  type DemoScenario
} from '../api/demo'

const router = useRouter()
const environment = ref<DemoEnvironment | null>(null)
const samples = ref<DemoSample[]>([])
const loading = ref(false)
const importing = ref(false)
const error = ref('')

const labels: Record<DemoScenario, string> = {
  normal: '正常报告',
  document: '文档溯源',
  recovery: '服务异常恢复'
}
const scenarioType: Record<DemoScenario, 'success' | 'primary' | 'warning'> = {
  normal: 'success',
  document: 'primary',
  recovery: 'warning'
}
const environmentTitle = computed(() =>
  environment.value?.execution_mode === 'online' ? '在线环境已配置' : '离线演示环境'
)
const environmentType = computed<'success' | 'info'>(() =>
  environment.value?.execution_mode === 'online' ? 'success' : 'info'
)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [env, list] = await Promise.all([fetchDemoEnvironment(), fetchDemoSamples()])
    environment.value = env
    samples.value = list
  } catch {
    error.value = '演示样例暂不可用，请检查后端服务后重试'
  } finally {
    loading.value = false
  }
}

async function importSamples(ids?: string[]) {
  if (importing.value || authRole.value !== 'admin') return
  importing.value = true
  error.value = ''
  try {
    const result = await importDemoSamples(ids)
    ElMessage.success(result.imported.length
      ? `已导入 ${result.imported.length} 个演示样例（不会启动生成）`
      : '演示样例已存在，未重复写入')
    await load()
  } catch (e: any) {
    error.value = e?.response?.data?.error?.message || '演示样例导入失败，未改写用户数据'
  } finally {
    importing.value = false
  }
}

function openSample(sample: DemoSample) {
  router.push(sample.scenario === 'recovery' ? `/tasks/${sample.task_id}` : `/report/${sample.task_id}`)
}

onMounted(load)
</script>

<template>
  <el-card v-loading="loading" shadow="never" class="demo-card" data-testid="demo-panel">
    <template #header>
      <div class="demo-header">
        <div>
          <span class="card-title">演示流程</span>
          <el-tag size="small" type="warning" effect="plain">显式导入 · 不调用模型</el-tag>
        </div>
        <div class="demo-header-actions">
          <el-button
            v-if="authRole === 'admin'"
            size="small"
            type="primary"
            :loading="importing"
            @click="importSamples()"
          >
            导入全部演示
          </el-button>
          <el-button size="small" :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>
    </template>

    <p v-if="error" role="alert" class="demo-error">{{ error }}</p>
    <el-alert
      v-if="environment"
      data-testid="demo-environment"
      :title="environmentTitle"
      :description="environment.message"
      :type="environmentType"
      :closable="false"
      show-icon
      class="env-alert"
    />

    <div class="demo-list">
      <div
        v-for="sample in samples"
        :key="sample.sample_id"
        class="demo-item"
        :data-sample="sample.sample_id"
      >
        <div class="demo-item-head">
          <span class="demo-title">{{ sample.title }}</span>
          <el-tag size="small" :type="scenarioType[sample.scenario]">{{ labels[sample.scenario] }}</el-tag>
          <el-tag size="small" :type="sample.imported ? 'success' : 'info'" effect="plain">
            {{ sample.imported ? '已导入' : '未导入' }}
          </el-tag>
        </div>
        <p class="demo-desc">{{ sample.description }}</p>
        <div class="demo-actions">
          <el-button v-if="sample.imported" size="small" @click="openSample(sample)">查看演示</el-button>
          <el-button
            v-else
            size="small"
            :disabled="authRole !== 'admin'"
            :loading="importing"
            @click="importSamples([sample.sample_id])"
          >
            导入此样例
          </el-button>
          <span v-if="!sample.imported && authRole !== 'admin'" class="demo-hint">管理员可导入</span>
        </div>
      </div>
      <el-empty v-if="!loading && !samples.length" description="暂无演示样例" :image-size="60" />
    </div>

    <p class="demo-note">
      演示数据来自仓库内固定样例，仅用于展示正常报告、文档溯源和服务恢复页面；不代表在线模型效果。
    </p>
  </el-card>
</template>

<style scoped>
.demo-card {
  margin-bottom: 24px;
}
.demo-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.card-title {
  font-weight: 600;
  font-size: 15px;
  margin-right: 8px;
}
.demo-header-actions {
  display: flex;
  gap: 8px;
}
.env-alert {
  margin-bottom: 14px;
}
.demo-error {
  color: #b42318;
  font-size: 13px;
  margin: 0 0 12px;
}
.demo-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.demo-item {
  padding: 12px 14px;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #fafafa;
}
.demo-item-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.demo-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}
.demo-desc {
  margin: 8px 0;
  color: #606266;
  font-size: 13px;
  line-height: 1.6;
}
.demo-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.demo-hint {
  color: #909399;
  font-size: 12px;
}
.demo-note {
  margin: 14px 0 0;
  color: #909399;
  font-size: 12px;
  line-height: 1.6;
}
</style>
