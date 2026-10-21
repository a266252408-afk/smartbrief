import { createHttp } from './http'

export type DemoScenario = 'normal' | 'document' | 'recovery'

/** 服务端显式样例摘要；imported 只表示本地是否存在，不代表在线生成。 */
export interface DemoSample {
  sample_id: string
  task_id: string
  title: string
  description: string
  scenario: DemoScenario
  sample_version: string
  imported: boolean
}

export interface DemoImportResult {
  imported: DemoSample[]
  skipped: DemoSample[]
}

export interface DemoEnvironment {
  demo_available: boolean
  demo_count: number
  llm_configured: boolean
  embedding_configured: boolean
  execution_mode: 'online' | 'offline'
  message: string
}

const http = createHttp({ baseURL: '/api/v2', timeout: 15000 })

export async function fetchDemoSamples(): Promise<DemoSample[]> {
  const { data } = await http.get('/demo/samples')
  return data.data.items as DemoSample[]
}

export async function fetchDemoEnvironment(): Promise<DemoEnvironment> {
  const { data } = await http.get('/demo/environment')
  return data.data as DemoEnvironment
}

export async function importDemoSamples(sampleIds?: string[]): Promise<DemoImportResult> {
  const { data } = await http.post('/demo/import', sampleIds ? { sample_ids: sampleIds } : {})
  return data.data as DemoImportResult
}
