import { createHttp } from './http'

/** 评测中心 API 封装 */

/** 单个用例的执行结果 */
export interface EvalCaseResult {
  case_index: number
  topic: string
  keywords: string[]
  task_id: string
  success: boolean
  /** 耗时（秒）；失败用例为 null */
  duration_seconds: number | null
  /** ReAct 循环次数 = Action 日志条数 */
  react_loops: number
  /** 关键词覆盖率（0~1，命中数/总关键词数） */
  keyword_coverage: number
  /** 长期记忆命中条数 */
  memory_hits: number
  /** 是否因达循环上限被强制通过 */
  forced_pass: boolean
  error: string | null
}

/** 评测指标汇总（实测值 + 目标值 + 达标判定） */
export interface EvalMetrics {
  summary: {
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
  eval_id: string
  status: EvalReport['status']
  total_cases: number
  completed_cases: number
  error: string | null
  created_at: string
  updated_at: string
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

/** 触发自动化评测（后台执行已配置的冒烟用例，立即返回 eval_id） */
export async function runEvaluation(): Promise<{ eval_id: string; status: string; total_cases: number }> {
  const { data } = await http.post('/evaluation/run')
  return data as { eval_id: string; status: string; total_cases: number }
}

/** 历史评测报告列表 */
export async function fetchEvalReports(params: { page?: number; page_size?: number } = {}): Promise<EvalReportListResult> {
  const { data } = await http.get('/evaluation/reports', { params })
  return data as EvalReportListResult
}

/** 评测报告详情（执行中返回进度，完成后返回结果与指标） */
export async function fetchEvalReport(evalId: string): Promise<EvalReport> {
  const { data } = await http.get(`/evaluation/reports/${evalId}`)
  return data as EvalReport
}

/** 删除历史评测报告（运行中的评测后端返回 400，前端提示） */
export async function deleteEvalReport(evalId: string): Promise<{ eval_id: string; deleted: boolean }> {
  const { data } = await http.delete(`/evaluation/reports/${evalId}`)
  return data as { eval_id: string; deleted: boolean }
}