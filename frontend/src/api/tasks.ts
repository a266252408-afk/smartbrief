import { createHttp } from './http'

/** 执行与报告质量为两个独立事实。 */
export type ExecutionStatus = 'queued' | 'running' | 'paused' | 'completed' | 'failed' | 'cancelled'
export type QualityStatus = 'pending' | 'passed' | 'rejected' | 'unavailable' | 'not_run' | 'unknown'
export interface TaskOutcome {
  execution_status: ExecutionStatus
  quality_status: QualityStatus
  forced_output: boolean
  error_code: string | null
}

/** 服务端分配编号的任务来源；外部文字始终为非可信资料。 */
export interface Evidence {
  source_id: string
  task_id: string
  kind: 'web' | 'document'
  title: string
  excerpt: string
  retrieved_at: string
  url: string | null
  document_id?: string | null
  chunk_id?: string | null
  page?: number | null
  score?: number | null
  source_deleted: boolean
}

export interface Citation {
  source_id: string
  claim: string
  valid: boolean
  reason: string | null
}

/** 任务摘要（列表项） */
export interface TaskSummary extends TaskOutcome {
  task_id: string
  topic: string
  /** running 执行中 / paused 已暂停（服务中断遗留）/ completed 已完成 / failed 失败 */
  status: 'running' | 'paused' | 'completed' | 'failed'
  plan: Array<{ id: number; agent: string; task: string; depends_on: number[] }>
  current_step_index: number
  completed_steps: number[]
  created_at: string
  updated_at: string
}

/** ReAct 日志条目 */
export interface ReactLog {
  id: number
  task_id: string
  step_id: number
  log_type: 'Thought' | 'Action' | 'Observation'
  content: string
  created_at: string
}

/** 单轮审核记录（review_history 元素） */
export interface ReviewRound {
  round: number
  passed: boolean | null
  quality_status?: QualityStatus
  comments: string[]
}

/** 长期记忆命中：Planner 规划前检索到的相似历史报告 */
export interface MemoryHit {
  topic: string
  /** 历史报告的结构（# 开头章节标题） */
  structure?: string[]
  snippet?: string
  /** 相似度分数（0~1，越高越相似） */
  score: number
  task_id: string
  created_at: string
}

/** 任务详情（status 接口返回） */
export interface TaskStatus extends TaskSummary {
  document_ids?: string[]
  retrievals?: RetrievalRecord[]
  evidence?: Evidence[]
  citations?: Citation[]
  citation_errors?: string[]
  research_data: Record<string, unknown>
  draft_content: string
  final_report: string | null
  error: string | null
  react_logs: ReactLog[]
  // A2A 审核循环
  review_comments: string[]
  review_history?: ReviewRound[]
  iteration: number
  max_iterations: number
  passed: boolean
  forced_pass: boolean
  // 长期记忆命中
  memory_hits: MemoryHit[]
}

/** 任务列表响应 */
export interface TaskListResult {
  total: number
  page: number
  page_size: number
  items: TaskSummary[]
}

/** 提交任务响应 */
export interface GenerateResult {
  task_id: string
  status: string
  topic: string
}

/** 任务报告（「报告展示」页数据源） */
export interface TaskReport extends TaskOutcome {
  sources?: Evidence[]
  citations?: Citation[]
  citation_errors?: string[]
  task_id: string
  title: string
  content_markdown: string
  /** 图表图片（Analyst 图表 Agent 属后续扩展，当前为空数组） */
  charts: Array<{ title: string; image_base64: string }>
  executive_summary: {
    /** 总耗时（秒，created_at → updated_at；无法解析时为 null） */
    total_time: number | null
    /** ReAct 行动次数 */
    react_loops: number
    /** 长期记忆命中条数 */
    memory_hit_count: number
  }
}

const http = createHttp({
  baseURL: '/api/v1',
  timeout: 15000
})

/** 提交研究任务（返回 task_id，立即返回） */
export async function submitTask(topic: string): Promise<GenerateResult> {
  const { data } = await http.post('/generate', { topic })
  return data as GenerateResult
}

/** 任务列表（分页 + 状态过滤 + 主题搜索 + 时间排序） */
export async function fetchTasks(params: {
  page?: number
  page_size?: number
  status?: string
  /** 按主题模糊搜索（报告中心搜索框） */
  keyword?: string
  /** 时间排序：desc 最新在前 / asc 最久在前（报告中心） */
  order?: 'asc' | 'desc'
} = {}): Promise<TaskListResult> {
  const { data } = await http.get('/tasks', { params })
  return data as TaskListResult
}

/** 任务详情：Plan 进度 + ReAct 日志 */
export async function fetchTaskStatus(taskId: string): Promise<TaskStatus> {
  const { data } = await http.get(`/tasks/${taskId}/status`)
  return data as TaskStatus
}

/** 删除任务（级联删除其 ReAct 日志） */
export async function deleteTask(taskId: string): Promise<{ task_id: string; deleted: boolean }> {
  const { data } = await http.delete(`/tasks/${taskId}`)
  return data as { task_id: string; deleted: boolean }
}

/** 恢复任务：暂停/失败的任务从最近 checkpoint 断点续跑 */
export async function resumeTask(taskId: string): Promise<{ task_id: string; status: string; resumed: boolean }> {
  const { data } = await http.post(`/tasks/${taskId}/resume`)
  return data as { task_id: string; status: string; resumed: boolean }
}

/** 任务报告：最终报告 Markdown + 执行摘要（仅已完成任务可获取） */
export async function fetchTaskReport(taskId: string): Promise<TaskReport> {
  const { data } = await http.get(`/tasks/${taskId}/report`)
  return data as TaskReport
}

/** 上传文档检索的独立状态，不代表报告质量。 */
export interface RetrievalRecord {
  task_id: string
  document_ids: string[]
  query: string
  options: { top_k: number; threshold: number; context_budget: number }
  status: 'not_selected' | 'matched' | 'no_hits' | 'below_threshold' | 'budget_excluded' | 'documents_missing' | 'embedding_failed' | 'index_failed'
  evidence: Evidence[]
  error_code: string | null
}
