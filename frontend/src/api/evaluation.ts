import { createHttp } from './http'

/** 评测中心 API 封装 */

/** 单个用例的执行结果 */
export interface EvalCaseResult {
  case_id?: string
  citation_valid_rate?: number | null
  input_tokens?: number | null
  output_tokens?: number | null
  estimated_costs?: Record<string, number>
  cost_complete?: boolean
  case_index: number
  topic: string
  keywords: string[]
  task_id: string
  success: boolean
  execution_success?: boolean
  quality_passed?: boolean
  quality_status?: string
  forced_output?: boolean
  failure_category?: string | null
  /** 耗时（秒）；失败用例为 null */
  duration_seconds: number | null
  /** ReAct 循环次数 = Action 日志条数 */
  react_loops: number
  /** 关键词覆盖率（0~1，命中数/总关键词数） */
  keyword_coverage: number
  /** 长期记忆命中条数 */
  memory_hits: number
  /** 是否因达循环上限被强制输出 */
  forced_pass: boolean
  error: string | null
}

/** 评测指标汇总（实测值 + 目标值 + 达标判定） */
export interface EvalMetrics {
  quality?: {
    metric_version: number
    quality_pass_rate: number
    execution_completion_rate: number
    pending_cases: number
    citation_valid_rate: number | null
    input_tokens: number | null
    output_tokens: number | null
    estimated_costs: Record<string, number>
    cost_complete: boolean
    p95_completed_seconds: number | null
    failure_categories: Record<string, number>
    manual_fact_check: { status: string; sample_size: number; accuracy: number | null }
  }
  summary: {
    metric_version?: number
    execution_success_rate?: number
    execution_success_cases?: number
    total_cases: number
    success_cases: number
    failed_cases: number
    success_rate: number
    avg_duration_seconds: number
    p95_duration_seconds: number
    avg_react_loops: number
    keyword_coverage_rate: number
    memory_hit_rate: number
  }
  targets: Record<string, number>
  met: Record<string, boolean>
}

/** 评测报告（详情接口返回） */
export interface EvalReport {
  metadata?: EvalMetadata | null
  eval_id: string
  /** running（执行中）/ completed / failed */
  status: 'running' | 'completed' | 'failed'
  total_cases: number
  completed_cases: number
  results: EvalCaseResult[]
  metrics: EvalMetrics
  report_md: string
  error: string | null
  created_at: string
  updated_at: string
}

/** 评测报告摘要（列表项，不含大文本） */
export interface EvalReportSummary {
  metadata?: EvalMetadata | null
  eval_id: string
  status: EvalReport['status']
  total_cases: number
  completed_cases: number
  error: string | null
  created_at: string
  updated_at: string
}

export interface EvalMetadata {
  source_hash?: string | null
  mode: 'offline' | 'online'
  suite_version: string
  dataset_hash: string
  code_commit: string | null
  code_tag: string | null
  code_dirty: boolean | null
  model: string
  prompt_version: string
  feature_flags: { rag: boolean; memory: boolean; review: boolean }
  randomness: string
  memory_protocol: string
  evidence_scope: string
  online_experiment: { status: string; results: null }
}

/** 评测列表响应 */
export interface EvalReportListResult {
  total: number
  page: number
  page_size: number
  items: EvalReportSummary[]
}

const http = createHttp({
  baseURL: '/api/v1',
  timeout: 15000
})
const v2 = createHttp({ baseURL: '/api/v2', timeout: 15000 })

/** 触发自动化评测（后台执行已配置的冒烟用例，立即返回 eval_id） */
export async function runEvaluation(featureFlags = { rag: true, memory: true, review: true }): Promise<{ eval_id: string; status: string; total_cases: number }> {
  const { data } = await v2.post('/evaluations', { suite_version: 'protocol-1', mode: 'offline', feature_flags: featureFlags })
  return data.data
}

/** 历史评测报告列表 */
export async function fetchEvalReports(params: { page?: number; page_size?: number } = {}): Promise<EvalReportListResult> {
  const { data } = await v2.get('/evaluations', { params })
  return data.data as EvalReportListResult
}

/** 评测报告详情（执行中返回进度，完成后返回结果与指标） */
export async function fetchEvalReport(evalId: string): Promise<EvalReport> {
  const { data } = await v2.get(`/evaluations/${encodeURIComponent(evalId)}`)
  return data.data as EvalReport
}

/** 删除历史评测报告（运行中的评测后端返回 400，前端提示） */
export async function deleteEvalReport(evalId: string): Promise<{ eval_id: string; deleted: boolean }> {
  const { data } = await http.delete(`/evaluation/reports/${evalId}`)
  return data as { eval_id: string; deleted: boolean }
}

export interface EvaluationComparison {
  changed_feature: string
  quality_pass_rate_delta: number
  execution_completion_rate_delta: number
  interpretation: string
}

export async function compareEvaluations(evalId: string, baseline: string): Promise<EvaluationComparison> {
  const { data } = await v2.get(`/evaluations/${encodeURIComponent(evalId)}/comparison`, { params: { baseline } })
  return data.data
}
