import { createHttp } from './http'

export interface CatalogCase {
  case_id: string; collection: string; area: string; title: string; split: string
  human_review: string; run_status: string; source_count: number; has_fixture: boolean
}
export interface CatalogArea {
  area: string; label: string; case_count: number; runnable: boolean
  profile: string | null; availability: string; score: number | null
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
