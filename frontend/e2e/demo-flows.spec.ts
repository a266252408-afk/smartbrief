import { test, expect, type Page } from '@playwright/test'
import { readFile } from 'node:fs/promises'

// 三条演示流程使用明确HTTP夹具；真实导入/引用/仓储由后端集成测试覆盖。

interface Sample { sample_id: string; task_id: string; title: string; description: string
  scenario: 'normal' | 'document' | 'recovery'; sample_version: string; imported: boolean }

const samples: Sample[] = [
  { sample_id: 'normal-report', task_id: 'demo-normal-report', title: '正常报告演示样例',
    description: '正常报告流程说明', scenario: 'normal', sample_version: '1.0', imported: false },
  { sample_id: 'document-trace', task_id: 'demo-document-trace', title: '文档溯源演示样例',
    description: '上传文档溯源流程说明', scenario: 'document', sample_version: '1.0', imported: false },
  { sample_id: 'service-recovery', task_id: 'demo-service-recovery', title: '服务中断恢复演示样例',
    description: '服务失败后恢复流程说明', scenario: 'recovery', sample_version: '1.0', imported: false }
]

async function setup(page: Page, imported: string[] = []) {
  const unexpected: string[] = []
  const state = { samples: samples.map(sample => ({ ...sample, imported: imported.includes(sample.sample_id) })), mutations: [] as string[] }
  await page.route('**/*', async route => {
    if (new URL(route.request().url()).origin === 'http://127.0.0.1:5175') await route.continue()
    else { unexpected.push(route.request().url()); await route.abort() }
  })
  await page.route('**/api/v*/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v2/auth/session') {
      await route.fulfill({ json: { data: { role: 'admin' }, meta: { request_id: 'fixture' } } })
    } else if (path === '/api/v2/demo/environment') {
      await route.fulfill({ json: { data: { demo_available: true, demo_count: 3, llm_configured: false,
        embedding_configured: false, execution_mode: 'offline',
        message: '未配置模型 Key：可导入演示样例查看完整页面，在线生成不可用。' }, meta: { request_id: 'fixture' } } })
    } else if (path === '/api/v2/demo/samples') {
      await route.fulfill({ json: { data: { items: state.samples }, meta: { request_id: 'fixture' } } })
    } else if (path === '/api/v2/demo/import' && request.method() === 'POST') {
      state.mutations.push(path)
      expect(request.postDataJSON()).toEqual({})
      state.samples = state.samples.map(sample => ({ ...sample, imported: true }))
      await route.fulfill({ json: { data: { imported: state.samples, skipped: [] }, meta: { request_id: 'fixture' } } })
    } else {
      unexpected.push(path)
      await route.fulfill({ status: 500, json: { detail: '未声明的测试请求：' + path } })
    }
  })
  await page.goto('/home')
  await page.getByLabel('访问令牌').fill('fixture-demo')
  await page.getByRole('button', { name: '验证并进入' }).click()
  await expect(page.getByTestId('demo-panel')).toBeVisible()
  return { state, unexpected }
}

const normalSource = { source_id: 'S1', task_id: 'demo-normal-report', kind: 'web',
  title: '演示资料：智能手表市场摘要', excerpt: '演示数据：出货量同比增长 8%。',
  url: 'https://example.invalid/demo/smartwatch-market', retrieved_at: '2026-10-20T00:00:00Z',
  document_id: null, chunk_id: null, page: null, score: null, source_deleted: false }
const normalReport = {
  task_id: 'demo-normal-report', title: '正常报告演示样例', status: 'completed', execution_status: 'completed',
  quality_status: 'passed', forced_output: false, error_code: null,
  metadata: { demo: true, generator_version: 'demo-1.0', demo_sample_id: 'normal-report', demo_sample_version: '1.0' },
  generator_version: 'demo-1.0', content_markdown: '## 结论\n演示数据：出货量同比增长 8%。[S1]',
  sources: [normalSource], citations: [{ source_id: 'S1', claim: '演示数据：出货量同比增长 8%。', valid: true, reason: null }],
  citation_errors: [], charts: [], executive_summary: { total_time: 0, react_loops: 2, memory_hit_count: 0 }
}

test('正常报告流程：导入演示、查看质量与来源、导出保留生成版本', async ({ page }) => {
  const { state, unexpected } = await setup(page)
  await expect(page.getByTestId('demo-environment')).toContainText('离线演示环境')
  await expect(page.getByTestId('demo-environment')).toContainText('未配置模型 Key')
  await page.route('**/api/v1/tasks/demo-normal-report/report', route => route.fulfill({ json: normalReport }))

  await page.getByRole('button', { name: '导入全部演示' }).click()
  await expect(page.locator('[data-sample="normal-report"]')).toContainText('已导入')
  await expect(page.getByText('已导入 3 个演示样例（不会启动生成）')).toBeVisible()
  await page.locator('[data-sample="normal-report"]').getByRole('button', { name: '查看演示' }).click()
  await expect(page).toHaveURL(/\/report\/demo-normal-report$/)
  await expect(page.getByTestId('quality-status')).toHaveText('审核通过')
  await expect(page.getByTestId('demo-badge')).toHaveText('演示数据')
  await expect(page.getByTestId('demo-notice')).toContainText('不代表在线生成成绩')
  await expect(page.locator('#source-S1')).toContainText('演示数据：出货量同比增长 8%。')

  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出 Word', exact: true }).click()
  const html = await readFile((await (await download).path())!, 'utf8')
  expect(html).toContain('https://example.invalid/demo/smartwatch-market')
  expect(html).toContain('演示数据：出货量同比增长 8%。')
  expect(html).toContain('生成版本：demo-1.0')
  expect(html).toContain('演示数据（不代表在线生成）')
  expect(html).toContain('审核通过')
  expect(html).toContain('可追溯来源：1 条')
  expect(state.mutations).toEqual(['/api/v2/demo/import'])
  expect(unexpected).toEqual([])
})

const documentId = 'a'.repeat(32)
const documentSource = { source_id: 'S1', task_id: 'demo-document-trace', kind: 'document',
  title: '演示资料：蓝鲸项目交付记录.txt', excerpt: '演示数据：蓝鲸项目交付 137 台设备。',
  url: null, retrieved_at: '2026-10-20T00:00:00Z', document_id: documentId,
  chunk_id: 'demo-chunk-137', page: 2, score: 0.95, source_deleted: false }
const documentReport = {
  task_id: 'demo-document-trace', title: '文档溯源演示样例', status: 'completed', execution_status: 'completed',
  quality_status: 'passed', forced_output: false, error_code: null,
  metadata: { demo: true, generator_version: 'demo-1.0', demo_sample_id: 'document-trace', demo_sample_version: '1.0' },
  generator_version: 'demo-1.0', content_markdown: '## 检索结果\n根据演示上传资料，项目交付 137 台设备。[S1]',
  sources: [documentSource], citations: [{ source_id: 'S1', claim: '根据演示上传资料，项目交付 137 台设备。', valid: true, reason: null }],
  citation_errors: [], document_ids: [documentId],
  retrievals: [{ task_id: 'demo-document-trace', document_ids: [documentId], query: '蓝鲸项目交付数量',
    options: { top_k: 5, threshold: 0.35, context_budget: 6000 }, status: 'matched', error_code: null,
    evidence: [documentSource] }],
  charts: [], executive_summary: { total_time: 0, react_loops: 2, memory_hit_count: 0 }
}

test('上传文档溯源流程：检索片段、页码与引用可回溯', async ({ page }) => {
  const { unexpected } = await setup(page, ['document-trace'])
  await page.route('**/api/v1/tasks/demo-document-trace/report', route => route.fulfill({ json: documentReport }))
  await page.locator('[data-sample="document-trace"]').getByRole('button', { name: '查看演示' }).click()
  await expect(page).toHaveURL(/\/report\/demo-document-trace$/)
  await expect(page.locator('.retrieval-panel')).toContainText('已找到可用片段')
  await expect(page.locator('.retrieval-panel')).toContainText('页码 2')
  await expect(page.locator('.retrieval-panel')).toContainText('0.9500')
  await expect(page.locator('.retrieval-panel')).toContainText('demo-chunk-137')
  await page.locator('a.citation-link').click()
  await expect(page).toHaveURL(/#source-S1$/)
  await expect(page.locator('#source-S1')).toContainText(documentId)
  await expect(page.locator('#source-S1')).toContainText('137 台设备')
  await expect(page.getByTestId('demo-notice')).toContainText('演示数据')

  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出 Word', exact: true }).click()
  const html = await readFile((await (await download).path())!, 'utf8')
  expect(html).toContain(documentId)
  expect(html).toContain('demo-chunk-137')
  expect(html).toContain('生成版本：demo-1.0')
  expect(unexpected).toEqual([])
})

const recoveryTask = {
  task_id: 'demo-service-recovery', topic: '服务中断恢复演示（演示样例）', status: 'paused', execution_status: 'paused',
  quality_status: 'pending', forced_output: false, error_code: 'SERVICE_INTERRUPTED',
  error: '演示数据：服务中断后任务进入已暂停状态；恢复入口仅用于展示，不会执行真实生成。',
  metadata: { demo: true, generator_version: 'demo-1.0', demo_sample_id: 'service-recovery', demo_sample_version: '1.0' },
  plan: [{ id: 1, agent: 'researcher', task: '检索离线演示资料', depends_on: [] }],
  completed_steps: [1], current_step_index: 1, research_data: {}, draft_content: '', final_report: null,
  react_logs: [{ id: 1, task_id: 'demo-service-recovery', step_id: 1, log_type: 'Observation',
    content: '服务中断：任务已暂停，可查看恢复说明。', created_at: '' }],
  review_comments: [], review_history: [], iteration: 0, max_iterations: 3, passed: null,
  forced_pass: false, memory_hits: [], created_at: '', updated_at: ''
}

test('1280×800 桌面尺寸下关键入口可见且无横向遮挡', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  const { unexpected } = await setup(page, ['normal-report'])
  await expect(page.getByRole('heading', { name: '多智能体报告生成' })).toBeVisible()
  await expect(page.getByRole('button', { name: '生成研究报告' })).toBeVisible()
  await expect(page.getByTestId('demo-panel')).toBeVisible()
  await expect(page.locator('[data-sample="normal-report"]')).toBeVisible()
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  expect(unexpected).toEqual([])
})

test('服务中断恢复流程：说明原因与真实恢复入口，演示数据不执行恢复', async ({ page }) => {
  const { state, unexpected } = await setup(page, ['service-recovery'])
  await page.route('**/api/v1/tasks/demo-service-recovery/status', route => route.fulfill({ json: recoveryTask }))
  await page.route('**/api/v2/tasks/demo-service-recovery/usage', route => route.fulfill({ json: { data: {
    task_id: 'demo-service-recovery', call_count: 1, blocked_count: 0, unknown_calls: 1,
    input_tokens: null, output_tokens: null, duration_ms: 0, estimated_costs: {}, cost_complete: false,
    budget_limit_tokens: 100000, budget_charged_tokens: 0, budget_remaining_tokens: 100000, budget_estimated: true,
    items: [{ call_id: 'demo-service-recovery-1', task_id: 'demo-service-recovery', agent: 'researcher',
      model: 'demo/offline-fixture', status: 'failed', duration_ms: 0, input_tokens: null, output_tokens: null,
      usage_known: false, estimated_cost: null, currency: null, attempt: 1, reserved_tokens: 0,
      created_at: '2026-10-20T00:00:00+00:00', error_code: 'MODEL_TIMEOUT' }]
  }, meta: { request_id: 'fixture' } } }))
  await page.locator('[data-sample="service-recovery"]').getByRole('button', { name: '查看演示' }).click()
  await expect(page).toHaveURL(/\/tasks\/demo-service-recovery$/)
  await expect(page.locator('.status-tag')).toHaveText('已暂停')
  await expect(page.getByTestId('demo-badge')).toHaveText('演示数据')
  await expect(page.getByTestId('demo-notice')).toContainText('服务中断')
  await expect(page.getByTestId('demo-notice')).toContainText('恢复任务')
  await expect(page.getByText(/演示数据：服务中断后任务进入已暂停状态/)).toBeVisible()
  await expect(page.getByRole('button', { name: '恢复任务', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '取消任务', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '重新生成', exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '查看用量与费用' }).click()
  await expect(page.getByText('未知用量 1 次')).toBeVisible()
  await expect(page.getByText('费用未知：未配置必要单价，或供应商用量/缓存统计不完整')).toBeVisible()
  await expect(page.getByText('失败（MODEL_TIMEOUT）')).toBeVisible()
  expect(state.mutations).toEqual([])
  expect(unexpected).toEqual([])
})
