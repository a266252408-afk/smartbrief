<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadFile } from 'element-plus'
import {
  fetchDocuments,
  fetchDocumentDetail,
  deleteDocument,
  uploadDocument,
  searchKnowledge,
  searchMemory,
  fetchMemoryStatus,
  toggleMemory,
  type KnowledgeDocument,
  type DocumentDetail,
  type RagHit,
  type MemorySearchHit
} from '../api/knowledge'

const activeTab = ref<'docs' | 'search'>('docs')

// ---------------- 分类元数据 ----------------
// 统一顺序与文案：模板 → 术语 → 文档
const categoryMeta: Record<string, { label: string; type: 'primary' | 'success' | 'warning' | 'info' }> = {
  template: { label: '模板', type: 'primary' },
  terminology: { label: '术语', type: 'warning' },
  upload: { label: '文档', type: 'success' }
}

const categoryOrder = ['template', 'terminology', 'upload']

function categoryOf(category: string) {
  return categoryMeta[category] ?? { label: category, type: 'info' as const }
}

// ---------------- 文档列表 ----------------

const documents = ref<KnowledgeDocument[]>([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    documents.value = await fetchDocuments()
  } catch {
    ElMessage.error('获取知识库列表失败，请检查后端服务')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load()
  loadMemoryStatus()
})

const categoryCounts = computed(() => {
  const counts: Record<string, number> = { template: 0, terminology: 0, upload: 0 }
  for (const doc of documents.value) {
    counts[doc.category] = (counts[doc.category] ?? 0) + doc.chunk_count
  }
  return counts
})

// ---------------- 长期记忆开关 ----------------

const memoryEnabled = ref<boolean>(true)
const memoryCount = ref<number>(0)
const memoryLoading = ref<boolean>(false)

async function loadMemoryStatus() {
  memoryLoading.value = true
  try {
    const status = await fetchMemoryStatus()
    memoryEnabled.value = status.enabled
    memoryCount.value = status.count
  } catch {
    memoryEnabled.value = false
    memoryCount.value = 0
  } finally {
    memoryLoading.value = false
  }
}

async function onMemoryToggle(value: boolean | string | number) {
  const enabled = Boolean(value)
  try {
    const result = await toggleMemory(enabled)
    memoryEnabled.value = result.enabled
    ElMessage.success(result.message)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '切换失败')
    // 回滚 UI 状态
    memoryEnabled.value = !enabled
  }
}

// ---------------- 文档库列表筛选 ----------------

const listFilter = ref('') // 空表示全部

const listFilterOptions = [
  { value: '', label: '全部' },
  { value: 'template', label: '模板' },
  { value: 'terminology', label: '术语' },
  { value: 'upload', label: '文档' }
]

const filteredDocuments = computed(() => {
  if (!listFilter.value) return documents.value
  return documents.value.filter((d) => d.category === listFilter.value)
})

// ---------------- 文档详情 ----------------

const detailVisible = ref(false)
const detailLoading = ref(false)
const detailDoc = ref<KnowledgeDocument | null>(null)
const detailData = ref<DocumentDetail | null>(null)

async function openDetail(row: KnowledgeDocument) {
  detailDoc.value = row
  detailData.value = null
  detailVisible.value = true
  detailLoading.value = true
  try {
    detailData.value = await fetchDocumentDetail(row.source, row.category, row.document_id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '读取文档详情失败')
    detailVisible.value = false
  } finally {
    detailLoading.value = false
  }
}

// ---------------- 删除 ----------------

async function handleDelete(row: KnowledgeDocument) {
  try {
    const result = await deleteDocument(row.source, row.category, row.document_id)
    ElMessage.success(`已删除 ${result.deleted} 个 chunk`)
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

function confirmDelete(row: KnowledgeDocument) {
  ElMessageBox.confirm(
    `确定要删除「${row.source}」吗？（分类：${categoryOf(row.category).label}，标识：${row.document_id?.slice(0, 8) || '旧资料'}）`,
    '删除确认',
    { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }
  ).then(() => handleDelete(row)).catch(() => {})
}

// ---------------- 上传 ----------------

const uploadCategory = ref('upload')
const uploading = ref(false)

async function onPickFile(uploadFile: UploadFile) {
  if (!uploadFile.raw) {
    ElMessage.error('未获取到文件内容，请重新选择')
    return
  }
  uploading.value = true
  try {
    const result = await uploadDocument(uploadFile.raw, uploadCategory.value)
    ElMessage.success(
      `「${result.source}」入库成功：新增 ${result.added} 块 / 共 ${result.chunks} 块`
    )
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '上传失败，请检查文件格式')
  } finally {
    uploading.value = false
  }
  return false // 阻止 el-upload 自动上传
}

// ---------------- RAG / 记忆检索测试 ----------------

const ragQuery = ref('')
const ragCategory = ref('')
const ragSearching = ref(false)
const ragHits = ref<RagHit[]>([])
const ragSearched = ref(false)

async function doRagSearch() {
  const q = ragQuery.value.trim()
  if (!q) {
    ElMessage.warning('请输入检索文本')
    return
  }
  ragSearching.value = true
  try {
    const result = await searchKnowledge(q, ragCategory.value || undefined)
    ragHits.value = result.hits
    ragSearched.value = true
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '检索失败，请检查后端服务')
  } finally {
    ragSearching.value = false
  }
}

const memoryQuery = ref('')
const memorySearching = ref(false)
const memoryHits = ref<MemorySearchHit[]>([])
const memorySearched = ref(false)

async function doMemorySearch() {
  const q = memoryQuery.value.trim()
  if (!q) {
    ElMessage.warning('请输入检索主题')
    return
  }
  memorySearching.value = true
  try {
    const result = await searchMemory(q)
    memoryHits.value = result.hits
    memorySearched.value = true
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '记忆检索失败，请检查后端服务')
  } finally {
    memorySearching.value = false
  }
}

/** 分数渲染：Chroma cosine 相似度裁剪到 0~100% */
function scorePercent(score: number) {
  const pct = Math.max(0, Math.min(1, score)) * 100
  return `${pct.toFixed(0)}%`
}

function scoreType(score: number) {
  if (score >= 0.7) return 'success' as const
  if (score >= 0.45) return 'warning' as const
  return 'info' as const
}
</script>

<template>
  <div>
    <div class="kb-header">
      <div>
        <h2>知识库（RAG）</h2>
        <p class="hint">
          管理报告模板、行业术语和参考文档，生成报告时自动检索相关资料；
          开启长期记忆后，可复用历史报告辅助研究规划
        </p>
      </div>
      <div class="header-actions">
        <el-button :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <!-- 分类统计 -->
    <div class="stat-row">
      <div v-for="key in categoryOrder" :key="key" class="stat-card">
        <el-tag :type="categoryMeta[key].type" size="small" effect="plain">{{ categoryMeta[key].label }}</el-tag>
        <span class="stat-num">{{ categoryCounts[key] ?? 0 }}</span>
        <span class="stat-unit">块</span>
      </div>
      <div class="stat-card memory-stat">
        <el-tag :type="memoryEnabled ? 'warning' : 'info'" size="small" effect="plain">记忆</el-tag>
        <el-switch
          v-model="memoryEnabled"
          :loading="memoryLoading"
          inline-prompt
          active-text="开"
          inactive-text="关"
          @change="onMemoryToggle"
        />
        <span class="stat-num memory-count">{{ memoryEnabled ? memoryCount : '—' }}</span>
        <span class="stat-unit">篇</span>
      </div>
    </div>

    <el-tabs v-model="activeTab">
      <!-- ============ 文档库 ============ -->
      <el-tab-pane label="文档库" name="docs">
        <!-- 文档分类筛选 -->
        <div class="filter-bar">
          <span class="filter-label">分类筛选：</span>
          <el-radio-group v-model="listFilter" size="small">
            <el-radio-button v-for="opt in listFilterOptions" :key="opt.value" :label="opt.value">
              {{ opt.label }}
            </el-radio-button>
          </el-radio-group>
          <span class="hint">共 {{ filteredDocuments.length }} 条 / 总计 {{ documents.length }} 条</span>
        </div>

        <!-- 上传区域 -->
        <div class="upload-bar">
          <span class="filter-label">上传分类：</span>
          <el-radio-group v-model="uploadCategory" size="small">
            <el-radio-button label="template">模板</el-radio-button>
            <el-radio-button label="terminology">术语</el-radio-button>
            <el-radio-button label="upload">文档</el-radio-button>
          </el-radio-group>
          <el-upload
            :show-file-list="false"
            :auto-upload="false"
            accept=".txt,.md,.markdown,.docx,.pdf,.xlsx,.xls"
            :on-change="onPickFile"
            :disabled="uploading"
          >
            <el-button type="primary" :loading="uploading">上传文件</el-button>
          </el-upload>
          <span class="hint">
            支持 txt / md / docx / pdf / xlsx，自动解析 → 递归分块 → 向量化入库
          </span>
        </div>

        <el-table
          v-loading="loading"
          :data="filteredDocuments"
          class="kb-table"
          highlight-current-row
          @row-click="openDetail"
        >
          <el-table-column label="分类" width="90">
            <template #default="{ row }">
              <el-tag :type="categoryOf(row.category).type" size="small">
                {{ categoryOf(row.category).label }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="source" label="文档来源" min-width="200" show-overflow-tooltip />
          <el-table-column prop="document_id" label="文档标识" min-width="240" show-overflow-tooltip />
          <el-table-column prop="file_type" label="类型" width="120" />
          <el-table-column prop="chunk_count" label="分块数" width="90" />
          <el-table-column prop="snippet" label="内容摘要（首块）" min-width="260" show-overflow-tooltip />
          <el-table-column label="操作" width="110">
            <template #default="{ row }">
              <el-button link type="primary" @click.stop="openDetail(row)">查看</el-button>
              <el-button link type="danger" @click.stop="confirmDelete(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!loading && filteredDocuments.length === 0" :description="documents.length === 0 ? '知识库为空（后端启动时自动 seed 模板与术语）' : '该分类下暂无文档'" />

        <!-- 文档详情弹窗 -->
        <el-dialog
          v-model="detailVisible"
          :title="detailDoc ? `${detailDoc.source} — 文档详情` : '文档详情'"
          width="720px"
          top="3vh"
          destroy-on-close
        >
          <div v-loading="detailLoading" class="detail-body">
            <div v-if="detailData" class="detail-meta">
              <el-tag :type="categoryOf(detailData.category).type" size="small">
                {{ categoryOf(detailData.category).label }}
              </el-tag>
              <code>{{ detailData.source }}</code>
              <span class="hint">{{ detailData.chunks.length }} 个 chunk</span>
            </div>
            <div v-if="detailData" class="detail-chunks">
              <div v-for="(chunk, idx) in detailData.chunks" :key="chunk.id" class="detail-chunk">
                <div class="chunk-head">
                  <span class="chunk-index">#{{ idx + 1 }}</span>
                  <code class="chunk-id">{{ chunk.id }}</code>
                </div>
                <pre class="chunk-content">{{ chunk.content }}</pre>
                <details class="chunk-meta">
                  <summary>元数据</summary>
                  <pre class="meta-content">{{ JSON.stringify(chunk.metadata, null, 2) }}</pre>
                </details>
              </div>
            </div>
          </div>
        </el-dialog>
      </el-tab-pane>

      <!-- ============ 检索测试 ============ -->
      <el-tab-pane label="检索测试" name="search">
        <div class="search-block">
          <h3 class="search-title">RAG 知识库检索（Writer 注入术语的数据源）</h3>
          <div class="search-bar">
            <el-input
              v-model="ragQuery"
              placeholder="输入检索文本，例如：便携式储能电池的市场规模"
              clearable
              @keyup.enter="doRagSearch"
            />
            <el-select v-model="ragCategory" placeholder="全部分类" clearable style="width: 140px">
              <el-option label="模板" value="template" />
              <el-option label="术语" value="terminology" />
              <el-option label="上传" value="upload" />
            </el-select>
            <el-button type="primary" :loading="ragSearching" @click="doRagSearch">检索</el-button>
          </div>

          <div v-if="ragSearched" class="result-area">
            <div class="result-count hint">
              命中 {{ ragHits.length }} 条{{ ragHits.length === 0 ? '（空集合或未超阈值）' : '' }}
            </div>
            <div v-for="(hit, i) in ragHits" :key="i" class="result-item">
              <div class="result-head">
                <el-tag :type="categoryOf(hit.category).type" size="small">
                  {{ categoryOf(hit.category).label }}
                </el-tag>
                <el-tag size="small" effect="plain">相似度 {{ scorePercent(hit.score) }}</el-tag>
                <span class="result-source">{{ hit.term || hit.source }}</span>
              </div>
              <pre class="result-content">{{ hit.content }}</pre>
            </div>
          </div>
        </div>

        <div class="search-block memory-block">
          <h3 class="search-title">长期记忆检索（Planner 参考的历史报告）</h3>
          <div class="search-bar">
            <el-input
              v-model="memoryQuery"
              placeholder="输入主题，例如：家庭储能系统市场分析"
              clearable
              @keyup.enter="doMemorySearch"
            />
            <el-button type="warning" :loading="memorySearching" @click="doMemorySearch">检索记忆</el-button>
          </div>

          <div v-if="memorySearched" class="result-area">
            <div class="result-count hint">
              命中 {{ memoryHits.length }} 篇历史报告{{ memoryHits.length === 0 ? '（无相似记忆）' : '' }}
            </div>
            <div v-for="(hit, i) in memoryHits" :key="i" class="result-item">
              <div class="result-head">
                <span class="memory-dot">忆</span>
                <span class="memory-topic">{{ hit.topic }}</span>
                <el-tag :type="scoreType(hit.score)" size="small" effect="plain">
                  {{ scorePercent(hit.score) }}
                </el-tag>
                <span class="hint">{{ hit.created_at }}</span>
                <code class="memory-task-id">{{ hit.task_id }}</code>
              </div>
              <div v-if="hit.structure?.length" class="structure-row">
                <el-tag
                  v-for="(h, j) in hit.structure"
                  :key="j"
                  size="small"
                  type="info"
                  effect="plain"
                >{{ h }}</el-tag>
              </div>
              <pre class="result-content">{{ hit.snippet }}</pre>
            </div>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.kb-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 4px;
}
.kb-header h2 {
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
.stat-row {
  display: flex;
  gap: 12px;
  margin: 12px 0 4px;
}
.stat-card {
  display: flex;
  align-items: baseline;
  gap: 6px;
  background: #fff;
  border-radius: 8px;
  padding: 8px 16px;
  border: 1px solid #e4e7ed;
}
.stat-num {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
}
.stat-unit {
  font-size: 12px;
  color: #909399;
}
.memory-stat {
  gap: 10px;
}
.memory-count {
  margin-left: 4px;
}
.filter-bar,
.upload-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 8px 0 12px;
}
.filter-label {
  font-size: 13px;
  color: #606266;
}
.kb-table {
  width: 100%;
}
.search-block {
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 16px;
}
.memory-block {
  border-color: #f3d19e;
}
.search-title {
  margin: 0 0 12px;
  font-size: 15px;
  font-weight: 600;
  color: #303133;
}
.search-bar {
  display: flex;
  gap: 8px;
}
.result-area {
  margin-top: 12px;
}
.result-count {
  margin-bottom: 8px;
}
.result-item {
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 8px 12px;
  margin-bottom: 8px;
  background: #fafafa;
}
.result-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.result-source {
  color: #606266;
  font-size: 12px;
  font-family: monospace;
  word-break: break-all;
}
.result-content {
  margin: 0;
  padding: 8px;
  background: #f5f7fa;
  border-radius: 4px;
  font-size: 12px;
  color: #303133;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 120px;
  overflow: auto;
}
.memory-dot {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  line-height: 18px;
  text-align: center;
  border-radius: 50%;
  background: #e6a23c;
  color: #fff;
  font-size: 11px;
}
.memory-topic {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}
.memory-task-id {
  font-size: 11px;
  color: #909399;
}
.structure-row {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

/* 文档详情抽屉 */
.detail-body {
  padding: 8px 4px;
}
.detail-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid #e4e7ed;
}
.detail-chunks {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.detail-chunk {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 12px;
  background: #fafafa;
}
.chunk-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.chunk-index {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}
.chunk-id {
  font-size: 11px;
  color: #909399;
  word-break: break-all;
}
.chunk-content {
  margin: 0 0 8px;
  padding: 10px;
  background: #fff;
  border-radius: 4px;
  font-size: 13px;
  color: #303133;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 240px;
  overflow: auto;
}
.chunk-meta {
  font-size: 12px;
  color: #606266;
}
.chunk-meta summary {
  cursor: pointer;
  user-select: none;
}
.meta-content {
  margin: 6px 0 0;
  padding: 8px;
  background: #f5f7fa;
  border-radius: 4px;
  font-size: 11px;
  color: #606266;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 160px;
  overflow: auto;
}
</style>
