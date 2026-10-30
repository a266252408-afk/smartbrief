import { test, expect, type Page } from '@playwright/test'
import { readFile } from 'node:fs/promises'

// 明确HTTP夹具；真实HTTP→图→调用账本在后端test_usage_api独立验证。
async function setup(page: Page, mode: 'unknown' | 'known' | 'error') {
  let requests = 0
  const unexpected: string[] = []
  const row = { call_id: 'fixture', task_id: 'usage-fixture', agent: 'writer',
    model: '<img src=x onerror="window.__usageInjected=1">fixture-model', status: 'failed',
    duration_ms: 50, input_tokens: mode === 'known' ? 10 : null, output_tokens: mode === 'known' ? 5 : null,
    usage_known: mode === 'known', estimated_cost: mode === 'known' ? 0.00004 : null,
    currency: mode === 'known' ? 'USD' : null, attempt: 2, reserved_tokens: 300,
    created_at: '2026-10-21T00:00:00Z', error_code: 'MODEL_TIMEOUT' }
  const blocked = { ...row, call_id: 'blocked-fixture', status: 'budget_exceeded', duration_ms: 0,
    input_tokens: null, output_tokens: null, usage_known: false, estimated_cost: null,
    currency: null, attempt: 1, reserved_tokens: 0, error_code: 'BUDGET_EXCEEDED' }
  const data = { task_id: 'usage-fixture', items: [{ ...row, provider: '离线备用供应商', model_role: 'backup',
    cached_input_tokens: mode === 'known' ? 8 : null, cache_write_tokens: mode === 'known' ? 0 : null }, blocked], call_count: 1, blocked_count: 1,
    cached_input_tokens: mode === 'known' ? 8 : null, cache_write_tokens: mode === 'known' ? 0 : null,
    cache_unknown_calls: mode === 'known' ? 0 : 1,
    routing: { task_id: 'usage-fixture', role: 'backup', model: 'fixture-backup', provider: '离线备用供应商',
      reason: 'MODEL_CONNECTION', switched_at: 'fixture' },
    recovery: { resume_mode: 'checkpoint', next_agent: 'writer', next_step_id: 3, completed_steps: [1, 2] },
    unknown_calls: mode === 'known' ? 0 : 1, input_tokens: row.input_tokens, output_tokens: row.output_tokens,
    duration_ms: 50, estimated_costs: mode === 'known' ? { USD: 0.00004 } : {}, cost_complete: mode === 'known',
    budget_limit_tokens: 20000, budget_charged_tokens: 300, budget_remaining_tokens: 19700, budget_estimated: mode !== 'known' }
  await page.route('**/*', async route => {
    if (new URL(route.request().url()).origin === 'http://127.0.0.1:5175') await route.continue()
    else { unexpected.push(route.request().url()); await route.abort() }
  })
  await page.route('**/api/v*/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/auth/session')) await route.fulfill({ json: { data: { role: 'readonly' }, meta: { request_id: 'fixture' } } })
    else if (path.endsWith('/status')) await route.fulfill({ json: {
      task_id: 'usage-fixture', topic: '用量测试夹具', status: 'failed', execution_status: 'failed', quality_status: 'not_run',
      forced_output: false, error_code: 'BUDGET_EXCEEDED', plan: [], completed_steps: [], current_step_index: 0,
      research_data: {}, react_logs: [], draft_content: '', final_report: null, error: '任务Token预算不足',
      review_comments: [], review_history: [], memory_hits: [], iteration: 0, max_iterations: 3,
      evidence: [], created_at: '', updated_at: '' } })
    else if (path.endsWith('/usage')) {
      requests++
      await route.fulfill(mode === 'error' ? { status: 503, json: { error: { code: 'USAGE_UNAVAILABLE', message: '固定故障' } } } : { json: { data, meta: { request_id: 'fixture' } } })
    } else { unexpected.push(path); await route.fulfill({ status: 500 }) }
  })
  await page.goto('/tasks/usage-fixture')
  await page.getByLabel('访问令牌').fill('fixture-usage')
  await page.getByRole('button', { name: '验证并进入' }).click()
  await expect(page.getByRole('heading', { name: '用量测试夹具' })).toBeVisible()
  expect(requests).toBe(0)
  await page.getByRole('button', { name: '查看用量与费用' }).click()
  return { unexpected, data }
}

test('只读可查看未知Token、预算原因与失败尝试，模型文本不能执行', async ({ page }) => {
  const state = await setup(page, 'unknown')
  const panel = page.getByRole('region', { name: '模型用量与费用' })
  await expect(panel).toContainText('输入 Token：未知')
  await expect(panel).toContainText('费用未知')
  await expect(panel).toContainText('MODEL_TIMEOUT')
  await expect(panel.locator('img, script')).toHaveCount(0)
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).__usageInjected)).toBeUndefined()
  await page.screenshot({ path: '../docs/verification/T08-03-usage.png', fullPage: true })
  expect(state.unexpected).toEqual([])
})

test('有显式费用时展示并下载相同JSON账本', async ({ page }) => {
  const state = await setup(page, 'known')
  await expect(page.getByRole('region', { name: '模型用量与费用' })).toContainText('USD 0.00004000')
  await expect(page.getByRole('region', { name: '模型用量与费用' })).toContainText('本任务已绑定备用模型')
  await expect(page.getByRole('region', { name: '模型用量与费用' })).toContainText('写作 Agent · 步骤 3')
  await expect(page.getByRole('region', { name: '模型用量与费用' })).toContainText('缓存读取 Token：8')
  const event = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出用量 JSON' }).click()
  const download = await event
  expect(download.suggestedFilename()).toBe('usage-usage-fixture.json')
  const content = await readFile((await download.path())!, 'utf8')
  expect(JSON.parse(content)).toEqual(state.data)
  expect(content).not.toMatch(/PRIVATE_PROMPT|SECRET_KEY|"messages"|"prompt"/)
  expect(state.unexpected).toEqual([])
})

test('用量读取失败明确提示，不能显示空库或伪造零费用', async ({ page }) => {
  const state = await setup(page, 'error')
  const panel = page.getByRole('region', { name: '模型用量与费用' })
  await expect(panel.getByRole('alert')).toContainText('用量记录暂不可用')
  await expect(panel).not.toContainText('尚无调用记录')
  await expect(panel.getByRole('button', { name: '导出用量 JSON' })).toHaveCount(0)
  expect(state.unexpected).toEqual([])
})
