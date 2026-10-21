<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { submitTask } from '../api/tasks'
import { fetchDocuments, type KnowledgeDocument } from '../api/knowledge'
import DemoPanel from '../components/DemoPanel.vue'

const router = useRouter()
const topic = ref('')
const submitting = ref(false)
const documentIds = ref<string[]>([])
const documents = ref<KnowledgeDocument[]>([])
const documentsLoading = ref(false)
const documentsError = ref('')

async function loadDocuments(open: boolean) {
  if (!open || documentsLoading.value) return
  documentsLoading.value = true
  documentsError.value = ''
  try {
    documents.value = (await fetchDocuments()).filter(d => d.category === 'upload' && /^[a-f0-9]{32}$/.test(d.document_id ?? ''))
  } catch {
    documentsError.value = '资料列表读取失败，请重新打开选择框重试'
  } finally { documentsLoading.value = false }
}

async function onSubmit() {
  const value = topic.value.trim()
  if (!value) {
    ElMessage.warning('请输入研究主题')
    return
  }
  if (submitting.value) return
  submitting.value = true
  try {
    const result = documentIds.value.length ? await submitTask(value, documentIds.value) : await submitTask(value)
    ElMessage.success(`任务已进入队列：${result.task_id}`)
    router.push(`/tasks/${result.task_id}`)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.error?.message || e?.response?.data?.detail || '提交失败，请检查后端服务')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="home-page">
    <div class="hero">
      <h2>多智能体报告生成</h2>
      <p class="hint">
        输入研究主题，自动规划研究步骤、检索资料并撰写报告。
        支持查看执行进度、审核意见与报告结果
      </p>
    </div>

    <el-card shadow="never" class="submit-card">
      <el-input
        v-model="topic"
        type="textarea"
        :rows="3"
        maxlength="200"
        show-word-limit
        placeholder="例如：智能手表市场分析、便携式储能电源北美市场研究…"
        @keydown.enter.exact.prevent="onSubmit"
      />
      <p class="hint">选择上传文档作为事实资料（可选，最多 20 份）。模板用于规划，术语用于定义；旧资料缺少标识时需重新上传。</p>
      <el-select v-model="documentIds" multiple :multiple-limit="20" filterable clearable
        aria-label="选择上传文档" placeholder="选择上传文档" style="width: 100%"
        :loading="documentsLoading" @visible-change="loadDocuments">
        <el-option v-for="doc in documents" :key="doc.document_id" :value="doc.document_id!"
          :label="`${doc.display_name || doc.source} · ${doc.document_id?.slice(0, 8)}`" />
      </el-select>
      <p v-if="documentsError" role="alert">{{ documentsError }}</p>
      <div class="submit-actions">
        <el-button type="primary" size="large" :loading="submitting" @click="onSubmit">
          生成研究报告
        </el-button>
        <el-button size="large" @click="router.push('/tasks')">历史任务</el-button>
      </div>
    </el-card>

    <DemoPanel />

    <el-row :gutter="16" class="feature-row">
      <el-col :span="8">
        <el-card shadow="hover" class="feature-card">
          <h3>研究规划</h3>
          <p class="hint">根据研究主题制定执行计划，明确步骤与依赖关系</p>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover" class="feature-card">
          <h3>资料检索</h3>
          <p class="hint">围绕研究问题检索网络资料，记录搜索结果与来源</p>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover" class="feature-card">
          <h3>报告生成</h3>
          <p class="hint">结合研究资料生成结构化报告，并通过审核与返工完善内容</p>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.home-page {
  max-width: 960px;
  margin: 0 auto;
}
.hero {
  text-align: center;
  margin-bottom: 24px;
}
.hero h2 {
  margin: 0 0 8px;
  font-size: 24px;
}
.hint {
  color: #909399;
  font-size: 13px;
  line-height: 20px;
}
.submit-card {
  margin-bottom: 24px;
}
.submit-actions {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.feature-row .el-col {
  display: flex;
}
.feature-card {
  flex: 1;
}
.feature-row h3 {
  margin: 0 0 8px;
  font-size: 15px;
}
</style>
