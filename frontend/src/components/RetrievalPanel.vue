<script setup lang="ts">
import type { RetrievalRecord } from '../api/tasks'
defineProps<{ records?: RetrievalRecord[]; documentIds?: string[] }>()
const labels: Record<RetrievalRecord['status'], string> = {
  not_selected: '未选择文档', matched: '已找到可用片段', no_hits: '检索无命中',
  below_threshold: '命中相关性不足，未注入正文', budget_excluded: '片段超出上下文预算，未注入正文',
  documents_missing: '所选文档已删除或不存在，未注入正文',
  embedding_failed: '嵌入服务失败，未注入正文', index_failed: '文档索引失败，未注入正文'
}
</script>

<template>
  <el-card v-if="records?.length || documentIds?.length" shadow="never" class="retrieval-panel">
    <template #header>上传文档检索</template>
    <p v-if="!records?.length">检索记录尚不可用；不能据此认定已使用上传资料。</p>
    <article v-for="(record, i) in records" :key="i">
      <p><strong>{{ labels[record.status] || '检索状态未知' }}</strong></p>
      <p>所选文档：{{ record.document_ids.length }} 份 · 查询：{{ record.query }}</p>
      <p>最多 {{ record.options.top_k }} 个片段 · 最低相似度 {{ record.options.threshold }} · 上下文预算 {{ record.options.context_budget }} 字符</p>
      <article v-for="source in record.evidence" :key="source.chunk_id ?? source.source_id" class="retrieval-hit">
        <p>[{{ source.source_id }}] {{ source.title }} <strong v-if="source.source_deleted">（来源已删除，保留历史片段）</strong></p>
        <p>文档 {{ source.document_id }} · 片段 {{ source.chunk_id }} · 页码 {{ source.page ?? '不可获得' }} · 相似度 {{ source.score?.toFixed(4) ?? '未知' }}</p>
        <blockquote>{{ source.excerpt }}</blockquote>
      </article>
    </article>
    <p>相似度仅用于筛选相关资料；引用映射不证明事实真实性。</p>
  </el-card>
</template>

<style scoped>
.retrieval-panel { margin: 20px 0; }
.retrieval-hit { border-left: 3px solid #c5d8ed; padding-left: 16px; margin: 16px 0; overflow-wrap: anywhere; }
blockquote { white-space: pre-wrap; margin: 12px 0; }
</style>
