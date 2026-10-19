import { createHttp } from './http'

/** 知识库文档（列表项，按 (category, source) 聚合） */
export interface KnowledgeDocument {
  /** template | terminology | upload */
  category: string
  source: string
  /** 新上传文档标识；旧数据可能没有 */
  document_id?: string
  display_name?: string
  file_type: string
  chunk_count: number
  snippet: string
}

/** RAG 语义检索命中 */
export interface RagHit {
  content: string
  source: string
  category: string
  term: string
  score: number
}

/** 长期记忆检索命中（历史报告） */
export interface MemorySearchHit {
  topic: string
  structure: string[]
  snippet: string
  score: number
  task_id: string
  created_at: string
}

const http = createHttp({
  baseURL: '/api/v1',
  timeout: 20000
})

/** 知识库文档列表 */
export async function fetchDocuments(): Promise<KnowledgeDocument[]> {
  const { data } = await http.get('/knowledge/documents')
  return data.documents as KnowledgeDocument[]
}

/** 文档 chunk 详情 */
export interface DocumentChunk {
  id: string
  content: string
  metadata: Record<string, unknown>
}

/** 文档详情响应 */
export interface DocumentDetail {
  source: string
  category: string
  chunks: DocumentChunk[]
}

/** 获取知识库文档详情（按 category + source） */
export async function fetchDocumentDetail(
  source: string,
  category: string
): Promise<DocumentDetail> {
  const { data } = await http.get('/knowledge/document', {
    params: { source, category }
  })
  return data as DocumentDetail
}

/** 删除知识库文档（按 category + source） */
export async function deleteDocument(
  source: string,
  category: string
): Promise<{ deleted: number }> {
  const { data } = await http.delete('/knowledge/document', {
    params: { source, category }
  })
  return data as { deleted: number }
}

/** 上传文档到知识库（自动解析 → 分块 → 向量化入库） */
export async function uploadDocument(
  file: File,
  category = 'upload'
): Promise<{ source: string; document_id: string; display_name: string; category: string; added: number; chunks: number }> {
  const fd = new FormData()
  fd.append('file', file)
  fd.append('category', category)
  // 不手动设置 Content-Type：axios 对 FormData 自动带 boundary
  const { data } = await http.post('/knowledge/documents', fd)
  return data as { source: string; document_id: string; display_name: string; category: string; added: number; chunks: number }
}

/** RAG 检索测试（知识库内容） */
export async function searchKnowledge(
  query: string,
  category?: string,
  k = 5
): Promise<{ query: string; hits: RagHit[] }> {
  const { data } = await http.post('/knowledge/search', { query, category: category || null, k })
  return data as { query: string; hits: RagHit[] }
}

/** 长期记忆开关状态 */
export async function fetchMemoryStatus(): Promise<{ enabled: boolean; count: number }> {
  const { data } = await http.get('/memory/status')
  return data as { enabled: boolean; count: number }
}

/** 开启 / 关闭长期记忆 */
export async function toggleMemory(enabled: boolean): Promise<{ enabled: boolean; message: string }> {
  const { data } = await http.post('/memory/toggle', { enabled })
  return data as { enabled: boolean; message: string }
}

/** 长期记忆检索（历史任务报告） */
export async function searchMemory(
  query: string,
  k = 5
): Promise<{ query: string; hits: MemorySearchHit[] }> {
  const { data } = await http.get('/memory/search', { params: { q: query, k } })
  return data as { query: string; hits: MemorySearchHit[] }
}
