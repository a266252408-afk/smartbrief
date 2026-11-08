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
  excerpt_status?: 'unverified' | 'boundary_selected' | 'incomplete_tail'
}

export interface Citation {
  source_id: string
  claim: string
  valid: boolean
  reason: string | null
}

/** 任务元数据；旧任务为空对象，不能据此推断演示或生成版本。 */
export interface TaskMetadata {
  demo?: boolean
  generator_version?: string | null
  demo_sample_id?: string | null
  demo_sample_version?: string | null
  execution_mode?: 'online' | 'offline' | null
  evaluation_id?: string | null
  evaluation_case_id?: string | null
  report_pipeline_version?: 1 | 2 | null
}

/** 任务摘要（列表项） */
export interface TaskSummary extends TaskOutcome {
  resume_mode?: 'checkpoint' | 'restart' | null
  regenerated_from?: string | null
  metadata?: TaskMetadata
  task_id: string
  topic: string
  /** running 执行中 / paused 已暂停（服务中断遗留）/ completed 已完成 / failed 失败 */
  status: ExecutionStatus
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

/** 返工诊断记录的是程序决策，不能据此推断被拦截提案质量。 */
export interface RewriteVerification {
  finding_id: string
  reported_status?: string | null
  status: 'resolved' | 'unresolved' | 'unverified'
  reason: string
}
export interface RewriteAttempt {
  errors?: ProtocolDiagnostic[]
  normalization?: NormalizationTrace[]
  verification?: RewriteVerification[]
  mode: string
  decision?: string
  code?: string
  message?: string
  request?: { scope: string; allowed_paragraphs: number[]; reason?: string | null;
    evidence_ids?: string[]; missing_evidence_ids?: string[]; evidence_context?: string }
  accepted?: Array<{ paragraph: number; replacement: string }>
  rejected?: Array<{ index: number; code: string; message: string; proposal?: unknown;
    errors?: ProtocolDiagnostic[]; stage?: 'protocol' | 'content' }>
  raw_response?: string
  raw_truncated?: boolean
  raw_length?: number
}
export interface ProtocolDiagnostic {
  path: string
  type: string
  message: string
}
export interface NormalizationTrace {
  version: 1
  path: string
  action: 'text_to_replacement' | 'deduplicate_text'
  target: string
}
export interface RewriteAudit {
  normalization?: NormalizationTrace[]
  claim_bindings?: ClaimBinding[]
  fact_registry?: Record<string, BoundFact>
  citation_changes_applied?: boolean
  verification?: RewriteVerification[]
  model_results?: Array<{ finding_id: string; status: string; reason?: string }>
  citation_lifecycle?: { required_ids: string[]; retired_ids: string[];
    retired_claims: Array<{ source_id: string; claims: string[]; reason: string; review_required?: boolean }>;
    retained_claims?: Record<string, string[]> }
  mode: string
  content_index?: ContentIndex
  target_mapping?: Record<string, number | null>
  changed?: boolean
  failures?: string[]
  attempts?: RewriteAttempt[]
  program_restriction?: { observed: boolean; outcome: string; codes: string[]; quality_effect: string }
}

/** 单轮审核记录（review_history 元素） */
export interface ContentIndex {
  version: 1
  task_id: string
  revision: number
  body_sha256: string
  blocks: Array<{ target_id: string; paragraph: number; text: string; digest: string;
    certainty: 'new' | 'exact' | 'mapped' | 'unverified' }>
}

export interface RuleAssessment {
  origin: 'program' | 'model'
  decision: 'confirmed' | 'suspected' | 'dismissed' | 'unresolved'
  material: boolean
  decision_reason: string
}
export interface ReviewFinding extends Partial<RuleAssessment> {
  finding_id: string
  issue_id?: string
  issue_key?: string
  target_id?: string
  occurrence?: number
  identity_certainty?: string
  conflict_fields?: Record<string, unknown>
  rule?: string
  severity: 'hard' | 'soft' | 'info'
  problem: string
  expected: string
  evidence: { kind?: 'source' | 'report'; source_id?: string; quote: string }
  location: { paragraph: number; sentence: number }
}

export interface ReviewPolicy {
  version: 'review-policy-1'
  task_id: string
  required_sections: ['summary', 'body', 'conclusion']
  required_questions: string[]
  data_requirements: { minimum_points: 2; evidence_required: true; core_questions_required: true }
  length_guidance: { target: 1000; evidence_gap_may_be_shorter: true }
  blocking_rules: string[]
}

export interface ReviewRound {
  content_index?: ContentIndex
  issue_set?: Array<Partial<ReviewFinding>>
  findings?: Array<Partial<ReviewFinding>>
  review_policy?: ReviewPolicy | null
  report_pipeline_version?: number
  claim_bindings?: ClaimBinding[]
  fact_registry?: Record<string, BoundFact>
  dispute_review?: { status: 'unresolved' | 'reviewed'; initial_findings: unknown[];
    result?: unknown; error?: string }
  policy_corrections?: Array<{ review: unknown; violations: Array<{ code: string; text: string }> }>
  citation_retirements?: Array<{ source_id: string; claims: string[]; reason: string; review_required?: boolean }>
  convergence?: { resolved: string[]; introduced: string[]; persisting: string[];
    current_review_complete: boolean; stopped: boolean; basis: string;
    unverified?: string[]; previous_problem_count?: number; current_problem_count?: number }
  review_complete?: boolean
  action_plan?: { scope: string; finding_ids: string[]; comments: string[] }
  rejected_findings?: Array<{ code: string; finding: unknown }>
  stop_reason?: string | null
  convergence_stopped?: boolean
  review_diagnostics?: { code?: string; message?: string }
  rewrite?: RewriteAudit

  round: number
  passed: boolean | null
  quality_status?: QualityStatus
  comments: string[]
}

export interface ClaimBinding {
  paragraph: number
  text: string
  fact_ids: string[]
  source_ids: string[]
}
export interface BoundFact {
  fact_id: string
  task_id: string
  source_id: string
  quote: string
  source: Evidence
  measurements: Array<Record<string, unknown>>
}

/** 长期记忆命中：Planner 规划前检索到的相似历史报告 */
export interface MemoryHit {
  memory_id?: string
  verified?: boolean
  quality_status?: 'passed' | 'unknown'
  prompt_version?: string | null
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
  regenerated_from?: string | null
  metadata?: TaskMetadata
  generator_version?: string | null
  document_ids?: string[]
  retrievals?: RetrievalRecord[]
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

const controlHttp = createHttp({ baseURL: '/api/v2', timeout: 15000 })
const pendingKeys = new Map<string, string>()
async function taskAction<T>(path: string, body: object = {}): Promise<T> {
  const request = JSON.stringify([path, body])
  let key = pendingKeys.get(request)
  if (!key) {
    key = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')
    pendingKeys.set(request, key)
  }
  const { data } = await controlHttp.post(path, body, { headers: { 'Idempotency-Key': key } })
  pendingKeys.delete(request)
  return data.data as T
}

/** 提交研究任务（返回 task_id，立即返回） */
export async function submitTask(topic: string, documentIds: string[] = []): Promise<GenerateResult> {
  return taskAction<GenerateResult>('/tasks', documentIds.length ? { topic, document_ids: documentIds } : { topic })
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
export async function resumeTask(taskId: string): Promise<TaskResumed> {
  return taskAction<TaskResumed>(`/tasks/${encodeURIComponent(taskId)}/resume`)
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

/** 生成新任务并保留原任务溯源。 */
export async function regenerateTask(taskId: string): Promise<GenerateResult & { regenerated_from: string }> {
  return taskAction<GenerateResult & { regenerated_from: string }>(`/tasks/${encodeURIComponent(taskId)}/regenerate`)
}

/** T07逐调用用量；未知Token与费用显式null。 */
export interface ModelUsage {
  provider?: string | null
  model_role?: 'primary' | 'backup' | null
  logical_call_id?: string | null
  cached_input_tokens?: number | null
  cache_write_tokens?: number | null
  call_id: string
  task_id: string
  agent: string
  model: string
  status: 'running' | 'success' | 'failed' | 'cancelled' | 'budget_exceeded'
  duration_ms: number
  input_tokens: number | null
  output_tokens: number | null
  usage_known: boolean
  estimated_cost: number | null
  currency: string | null
  attempt: number
  reserved_tokens: number
  created_at: string
  error_code: string | null
}

export interface UsageReport {
  cached_input_tokens?: number | null
  cache_write_tokens?: number | null
  cache_unknown_calls?: number
  routing?: {
    task_id: string
    role: 'backup'
    model: string
    provider: string
    reason: string
    switched_at: string
  } | null
  recovery?: {
    resume_mode: 'checkpoint' | 'restart'
    next_agent: 'planner' | 'researcher' | 'writer' | 'reviewer' | 'rewriter' | 'research_revision' | null
    next_step_id: number | null
    completed_steps: number[]
  } | null
  task_id: string
  items: ModelUsage[]
  call_count: number
  blocked_count: number
  unknown_calls: number
  input_tokens: number | null
  output_tokens: number | null
  duration_ms: number
  estimated_costs: Record<string, number>
  cost_complete: boolean
  budget_limit_tokens: number
  budget_charged_tokens: number
  budget_remaining_tokens: number
  budget_estimated: boolean
}

export async function fetchTaskUsage(taskId: string): Promise<UsageReport> {
  const client = createHttp({ baseURL: '/api/v2', timeout: 15000 })
  const { data } = await client.get(`/tasks/${encodeURIComponent(taskId)}/usage`)
  return data.data as UsageReport
}

export interface TaskAccepted {
  task_id: string
  topic: string
  status: 'queued'
  execution_status: 'queued'
}

export interface TaskCancelled {
  task_id: string
  status: 'cancelled'
  execution_status: 'cancelled'
  cancelled: true
  already_cancelled: boolean
}

export interface TaskResumed {
  task_id: string
  status: 'queued' | 'running'
  execution_status: 'queued' | 'running'
  resumed: boolean
  resume_mode: 'checkpoint' | 'restart' | null
}

export async function cancelTask(taskId: string): Promise<TaskCancelled> {
  return taskAction<TaskCancelled>(`/tasks/${encodeURIComponent(taskId)}/cancel`)
}
