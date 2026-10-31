import { createHttp } from './http'

export interface CatalogCase {
  case_id: string; collection: string; area: string; title: string; split: string
  human_review: string; run_status: string; source_count: number; has_fixture: boolean
}
export interface CatalogArea {
  area: string; label: string; case_count: number; runnable: boolean
  profile: string | null; availability: string; score: number | null
  supported_case_ids?: string[]; experiment_case_ids?: string[]; live_case_ids?: string[]
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
  task_execution_status?: string; business_quality?: string; calls?: number | null; input_tokens?: number | null; output_tokens?: number | null
  provider_calls?: number | null; baseline?: MechanismResult
  arms?: { mode: string; attempts: MechanismResult[]; provider_calls: number | null; ever_passed: boolean; always_passed: boolean }[]
  report?: string; report_sha256?: string; grader_version?: string
  judgements?: { rubric_id: string; label: string; reason: string; evidence_refs: string[] }[]
  trace?: Record<string, unknown>[]
  metrics?: { returned_count: number | null; valid_url_count: number | null; unique_sources: number | null; exact_duplicate_count: number | null; dated_documents: number | null; unknown_period_documents: number | null; stale_documents: number | null; freshness_target: string | null; unavailable_content_count: number | null; network_calls: number; relevance_at_5: number | null; key_evidence_coverage: number | null; provider_estimated_cost_usd?: number | null }
  evidence?: { title: string; url: string | null; snippet?: string; excerpt?: string; published_at: string | null; data_period: string | null; origin: string; content_scope: string; content_available: boolean }[]
}
export interface MechanismRun {
  profile?: 'mechanism_regression' | 'search_replay' | 'search_live' | 'report_grading' | 'grader_calibration' | 'full_task_local' | 'full_task_live' | 'bounded_experiment'
  run_id: string; execution_status: 'running' | 'completed' | 'failed'; report_quality: string
  created_at: string; error: string | null; results: MechanismResult[]
  summary: { planned: number; processed: number; passed: number; failed: number; unknown: number }
  metadata?: { dataset_hash: string; model_group?: string; source_hash?: string; code_commit?: string; experiment_progress?: { case_id: string; phase: string; attempt: number; result: MechanismResult }[] }
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

export async function startFullTask(caseIds: string[], group: 'primary' | 'backup' | 'mixed'): Promise<MechanismRun> {
  return (await http.post('/runs', { profile: 'full_task_local', case_ids: caseIds, group })).data.data
}

export interface ComparisonInput { baseline_id: string; current_id: string; changed_dimension: 'code' | 'model_group' }
export interface ComparisonResult extends ComparisonInput {
  comparison_id: string; attribution_allowed: boolean; reasons: string[]; differences: string[]
  execution_pass_rate_delta: number | null; quality_pass_rate_delta: number | null; all_attempt_seconds_delta: number | null
  case_changes: { case_id: string; baseline: string; current: string; transition: string }[]; interpretation: string
}
export async function createComparison(body: ComparisonInput): Promise<ComparisonResult> {
  return (await http.post('/comparisons', body)).data.data
}
export async function fetchComparisons(): Promise<ComparisonResult[]> {
  return (await http.get('/comparisons')).data.data.items
}

export async function startExperiment(caseIds: string[], maxAttempts: number, callLimit: number): Promise<MechanismRun> {
  return (await http.post('/runs', { profile: 'bounded_experiment', case_ids: caseIds, max_attempts: maxAttempts, call_limit: callLimit })).data.data
}

export async function startLiveSearch(caseIds: string[], authorizationId: string): Promise<MechanismRun> {
  return (await http.post('/runs', { profile: 'search_live', case_ids: caseIds, authorization_id: authorizationId })).data.data
}
