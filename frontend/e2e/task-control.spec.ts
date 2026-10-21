import { test, expect, type Page } from '@playwright/test'

// 明确HTTP协议夹具；实际队列/竞争/持久化/检查点恢复由后端真实集成验证。
async function setup(page: Page, mode: 'queue' | 'checkpoint' | 'restart', readonly = false) {
  let reads = 0, cancelled = false, resumed = false, resumeRequests = 0
  const keys: string[] = [], unexpected: string[] = [], mutations: string[] = []
  function task() {
    const status = cancelled ? 'cancelled' : mode === 'queue' ? reads < 2 ? 'queued' : 'running' : resumed ? 'queued' : 'paused'
    return { task_id: 'control-fixture', topic: '任务控制夹具', status, execution_status: status,
      resume_mode: resumed ? mode : null, quality_status: cancelled ? 'not_run' : 'pending', forced_output: false,
      error_code: cancelled ? 'TASK_CANCELLED' : null, error: cancelled ? '用户已取消任务' : null,
      plan: [], completed_steps: [], current_step_index: 0, research_data: {}, react_logs: [],
      draft_content: '', final_report: null, review_comments: [], review_history: [], memory_hits: [],
      iteration: 0, max_iterations: 3, created_at: '', updated_at: '' }
  }
  await page.route('**/*', async route => {
    if (new URL(route.request().url()).origin === 'http://127.0.0.1:5175') await route.continue()
    else { unexpected.push(route.request().url()); await route.abort() }
  })
  await page.route('**/api/v*/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname
    if (path.endsWith('/auth/session')) await route.fulfill({ json: { data: { role: readonly ? 'readonly' : 'admin' }, meta: { request_id: 'fixture' } } })
    else if (path.endsWith('/status')) {
      reads++
      const snapshot = task()
      if (mode === 'queue' && !readonly && reads === 3) await new Promise(resolve => setTimeout(resolve, 1500))
      await route.fulfill({ json: snapshot })
    }
    else if (path === '/api/v2/demo/environment') await route.fulfill({ json: { data: { demo_available: true,
      demo_count: 0, llm_configured: false, embedding_configured: false, execution_mode: 'offline',
      message: '离线夹具' }, meta: { request_id: 'fixture' } } })
    else if (path === '/api/v2/demo/samples') await route.fulfill({ json: { data: { items: [] }, meta: { request_id: 'fixture' } } })
    else if (path === '/api/v2/tasks') {
      mutations.push(path)
      expect(req.headers()['idempotency-key']).toMatch(/^[a-f0-9]{32}$/)
      await route.fulfill({ status: 202, json: { data: { task_id: 'control-fixture', topic: '任务控制夹具', status: 'queued', execution_status: 'queued' }, meta: { request_id: 'fixture' } } })
    } else if (path.endsWith('/cancel')) {
      mutations.push(path); cancelled = true
      expect(req.headers()['idempotency-key']).toMatch(/^[a-f0-9]{32}$/)
      await route.fulfill({ json: { data: { task_id: 'control-fixture', status: 'cancelled', execution_status: 'cancelled', cancelled: true, already_cancelled: false }, meta: { request_id: 'fixture' } } })
    } else if (path.endsWith('/resume')) {
      mutations.push(path); resumeRequests++
      keys.push(req.headers()['idempotency-key']!)
      if (mode === 'restart' && resumeRequests === 1) {
        await route.fulfill({ status: 503, json: { error: { message: '固定恢复故障' } } })
      } else {
        resumed = true
        await route.fulfill({ status: 202, json: { data: { task_id: 'control-fixture', status: 'queued', execution_status: 'queued', resumed: true, resume_mode: mode }, meta: { request_id: 'fixture' } } })
      }
    } else { unexpected.push(path); await route.fulfill({ status: 500 }) }
  })
  await page.goto(mode === 'queue' && !readonly ? '/home' : '/tasks/control-fixture')
  await page.getByLabel('访问令牌').fill('fixture-control')
  await page.getByRole('button', { name: '验证并进入' }).click()
  return { keys, unexpected, mutations, readCount: () => reads }
}

test('排队持续轮询进入运行，取消后停止轮询且不能恢复', async ({ page }) => {
  const state = await setup(page, 'queue')
  await page.getByRole('textbox').fill('任务控制夹具')
  await page.getByRole('button', { name: '生成研究报告' }).click()
  await expect(page.locator('.status-tag')).toHaveText('排队中')
  await page.screenshot({ path: '../docs/verification/T08-03-queued.png', fullPage: true, animations: 'disabled' })
  await expect(page.locator('.status-tag')).toHaveText('执行中', { timeout: 7000 })
  await expect.poll(state.readCount).toBeGreaterThanOrEqual(3)
  await page.getByRole('button', { name: '取消任务', exact: true }).click()
  await expect(page.locator('.status-tag')).toHaveText('已取消')
  await expect(page.getByRole('button', { name: '恢复任务', exact: true })).toHaveCount(0)
  const count = state.readCount()
  await page.waitForTimeout(2200)
  await expect(page.locator('.status-tag')).toHaveText('已取消')
  expect(state.readCount()).toBe(count)
  expect(state.unexpected).toEqual([])
})

for (const mode of ['checkpoint', 'restart'] as const) {
  test(`恢复明确展示${mode === 'checkpoint' ? '断点续跑' : '从头重试'}，失败重试复用幂等键`, async ({ page }) => {
    const state = await setup(page, mode)
    await page.getByRole('button', { name: '恢复任务', exact: true }).click()
    if (mode === 'restart') {
      await expect(page.getByRole('alert').filter({ hasText: '固定恢复故障' })).toBeVisible()
      await page.getByRole('button', { name: '恢复任务', exact: true }).click()
      expect(state.keys).toHaveLength(2)
      expect(state.keys[0]).toBe(state.keys[1])
    }
    await expect(page.locator('.status-tag')).toHaveText('排队中')
    await expect(page.getByText(`本次恢复：${mode === 'checkpoint' ? '断点续跑' : '从头重试'}`, { exact: true })).toBeVisible()
    expect(state.unexpected).toEqual([])
  })
}

test('只读可见排队状态但不能取消，浏览器不发写请求', async ({ page }) => {
  const state = await setup(page, 'queue', true)
  await expect(page.locator('.status-tag')).toHaveText('排队中')
  await expect(page.getByRole('button', { name: '取消任务', exact: true })).toBeDisabled()
  expect(state.mutations).toEqual([])
  expect(state.unexpected).toEqual([])
})
