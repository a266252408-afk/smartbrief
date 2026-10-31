import { createHttp } from './http'

export interface CatalogCase {
  case_id: string; collection: string; area: string; title: string; split: string
  human_review: string; run_status: string; source_count: number; has_fixture: boolean
}
export interface CatalogArea {
  area: string; label: string; case_count: number; runnable: boolean
  profile: string | null; availability: string; score: number | null
  supported_case_ids?: string[]
}
export interface EvaluationCatalog {
  version: string; dataset_hash: string; cutoff: string; total_cases: number
  collections: Record<string, number>; areas: CatalogArea[]; cases: CatalogCase[]
}
export interface CatalogDetail {
  case: { case_id: string; title: string; input: { user_request: string }; oracle?: {
    expected_handling: string; assertions: { check_id: string; rubric_id: string; requirement: string; critical: boolean }[]
  }; legacy?: Record<string, unknown> }
  sources: { id: string; title: string; url: string; published: string | null; facts: { fact_id: string; statement: string }[] }[]
  rubrics: { id: string; title: string; criterion: string }[]
  fixture: Record<string, unknown> | null
}
const http = createHttp({ baseURL: '/api/v2/evaluation-center', timeout: 15000 })
export async function fetchCatalog(): Promise<EvaluationCatalog> {
  return (await http.get('/catalog')).data.data
}
export async function fetchCatalogCase(id: string): Promise<CatalogDetail> {
  return (await http.get(`/cases/${encodeURIComponent(id)}`)).data.data
}

export interface MechanismResult {
  case_id: string; check_status: 'pass' | 'fail' | 'unknown'; test_count?: number
  passed?: number; failed?: number; skipped?: number; report_quality?: string; duration_seconds?: number; error: string | null
  report?: string; report_sha256?: string; grader_version?: string
  judgements?: { rubric_id: string; label: string; reason: string; evidence_refs: string[] }[]
  trace?: Record<string, unknown>[]
  metrics?: { returned_count: number; valid_url_count: number; unique_sources: number; exact_duplicate_count: number; dated_documents: number; unknown_period_documents: number; stale_documents: number | null; freshness_target: string | null; unavailable_content_count: number; network_calls: number; relevance_at_5: number | null; key_evidence_coverage: number | null }
  evidence?: { title: string; url: string | null; snippet: string; published_at: string | null; data_period: string | null; origin: string; content_scope: string; content_available: boolean }[]
}
export interface MechanismRun {
  profile?: 'mechanism_regression' | 'search_replay' | 'report_grading' | 'grader_calibration'
  run_id: string; execution_status: 'running' | 'completed' | 'failed'; report_quality: string
  created_at: string; error: string | null; results: MechanismResult[]
  summary: { planned: number; processed: number; passed: number; failed: number; unknown: number }
  metadata?: { dataset_hash: string }
}
export async function startMechanism(caseIds: string[]): Promise<MechanismRun> {
  return (await http.post('/runs', { profile: 'mechanism_regression', case_ids: caseIds })).data.data
}
export async function startSearchReplay(caseIds: string[]): Promise<MechanismRun> {
  return (await http.post('/runs', { profile: 'search_replay', case_ids: caseIds })).data.data
}
export async function gradeReport(caseId: string, report: string): Promise<MechanismRun> {
  return (await http.post('/grade-report', { profile: 'report_grading', case_id: caseId, report })).data.data
}
export interface CalibrationSummary {
  run_id?: string
  total: number; unknown: number; false_accepts: number; false_rejects: number; accuracy: number | null
  groups: Record<string, { total: number; unknown: number; accuracy: number | null }>
  results: { case_id: string; expected: string; label: string; reason: string; split: string }[]
}
export async function calibrateGrader(): Promise<CalibrationSummary> {
  return (await http.post('/calibrate')).data.data
}
export async function fetchMechanismRuns(): Promise<MechanismRun[]> {
  return (await http.get('/runs')).data.data.items
}
export async function fetchMechanismRun(id: string): Promise<MechanismRun> {
  return (await http.get(`/runs/${encodeURIComponent(id)}`)).data.data
}

export interface EvaluationReview {
  review_id: string; target: 'case_definition' | 'run_result'; case_id: string; run_id: string | null
  dataset_hash: string; decision: 'pass' | 'fail' | 'unknown'; notes: string
  actor: string; recorded_at: string; supersedes: string | null
}
export type ReviewInput = Pick<EvaluationReview, 'target' | 'case_id' | 'run_id' | 'dataset_hash' | 'decision' | 'notes'>
export async function saveEvaluationReview(body: ReviewInput): Promise<EvaluationReview> {
  return (await http.post('/reviews', body)).data.data
}
export async function fetchEvaluationReviews(): Promise<EvaluationReview[]> {
  return (await http.get('/reviews')).data.data.items
}
