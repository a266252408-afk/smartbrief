import { test, expect, type Page } from '@playwright/test'

// 明确HTTP协议夹具；真实准入/删除/重生成另由后端真实图与仓储集成验证。
async function setup(page: Page, role: 'admin' | 'readonly' = 'admin', unavailable = false) {
  let deleted = false, secondReads = 0
  const mutations: string[] = [], unexpected: string[] = []
  const active = { memory_id: 'a'.repeat(64), task_id: 'old', topic: '原报告', status: 'active',
    verified: true, quality_status: 'passed', model_name: 'fixture-model', model_version: null,
    prompt_version: 'smartbrief-report-T06-v1', created_at: '2026-10-20T00:00:00Z', invalid_reason: null }
  const legacy = { ...active, memory_id: 'legacy_' + 'b'.repeat(32), task_id: 'legacy-task',
    topic: '<img src=x onerror="window.__memoryInjected=1">旧记忆', status: 'unverified',
    verified: false, quality_status: 'unknown', model_name: null, prompt_version: null, created_at: null }
  await page.route('**/*', async route => {
    if (new URL(route.request().url()).origin === 'http://127.0.0.1:5175') await route.continue()
    else { unexpected.push(route.request().url()); await route.abort() }
  })
  await page.route('**/api/v*/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname
    if (path.endsWith('/auth/session')) await route.fulfill({ json: { data: { role }, meta: { request_id: 'fixture' } } })
    else if (path === '/api/v1/knowledge/documents') await route.fulfill({ json: { documents: [] } })
    else if (path === '/api/v1/memory/status') await route.fulfill({ json: { enabled: true, count: 1 } })
    else if (path === '/api/v2/memory') {
      if (unavailable) { await route.fulfill({ status: 503, json: { error: { message: '固定记忆故障' } } }); return }
      const entries = [active, { ...legacy, status: deleted ? 'deleted' : 'unverified' }]
      const status = url.searchParams.get('status')
      const items = entries.filter(e => !status || e.status === status)
      await route.fulfill({ json: { data: { items, total: items.length, page: 1, page_size: 10,
        enabled: true, counts: { active: 1, unverified: deleted ? 0 : 1, deleted: deleted ? 1 : 0, invalid: 0, unavailable: 0 } }, meta: { request_id: 'fixture' } } })
    } else if (path === `/api/v2/memory/${legacy.memory_id}` && request.method() === 'DELETE') {
      mutations.push(path); deleted = true
      await route.fulfill({ json: { data: { deleted: true, status: 'deleted' }, meta: { request_id: 'fixture' } } })
    } else if (path === '/api/v2/tasks/old/regenerate' || path === '/api/v2/tasks/new/regenerate') {
      mutations.push(path)
      const first = path.includes('/old/')
      await route.fulfill({ json: { data: { task_id: first ? 'new' : 'new2', topic: '新报告',
        status: 'running', regenerated_from: first ? 'old' : 'new' }, meta: { request_id: 'fixture' } } })
    } else if (path === '/api/v1/tasks/new/status' || path === '/api/v1/tasks/new2/status') {
      const second = path.includes('/new2/'), running = second && ++secondReads === 1
      await route.fulfill({ json: { task_id: second ? 'new2' : 'new', regenerated_from: second ? 'new' : 'old',
        topic: second ? running ? '第二次生成中' : '第二次生成完成' : '新报告', status: running ? 'running' : 'completed',
        execution_status: running ? 'running' : 'completed', quality_status: running ? 'pending' : 'passed',
        forced_output: false, error_code: null, plan: [], completed_steps: [], current_step_index: 0,
        research_data: {}, react_logs: [], draft_content: '测试报告', final_report: running ? null : '测试报告',
        error: null, review_comments: [], review_history: [], memory_hits: [], iteration: 0, max_iterations: 3,
        evidence: [], created_at: '', updated_at: '' } })
    } else { unexpected.push(path); await route.fulfill({ status: 500 }) }
  })
  await page.goto('/knowledge')
  await page.getByLabel('访问令牌').fill('fixture-memory')
  await page.getByRole('button', { name: '验证并进入' }).click()
  await page.getByRole('tab', { name: '检索测试' }).click()
  await page.getByRole('button', { name: '管理长期记忆' }).click()
  return { mutations, unexpected }
}

test('旧记忆隔离展示、删除和新任务溯源，连续重生成后正常轮询', async ({ page }) => {
  const state = await setup(page)
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('历史未验证')
  await expect(dialog).toContainText('供应商版本 未知')
  await expect(dialog.locator('img, script')).toHaveCount(0)
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).__memoryInjected)).toBeUndefined()
  await dialog.locator('.el-select').click()
  await page.getByRole('option', { name: '历史未验证', exact: true }).click()
  await expect(dialog.locator('.el-table__body tr')).toHaveCount(1)
  await dialog.getByRole('button', { name: '删除记忆', exact: true }).click()
  await page.getByRole('button', { name: '删除', exact: true }).click()
  await expect(dialog.locator('.el-table__body tr')).toHaveCount(0)
  await dialog.locator('.el-select').click()
  await page.getByRole('option', { name: '全部状态', exact: true }).click()
  await dialog.locator('.el-table__body tr').filter({ hasText: '原报告' }).getByRole('button', { name: '重新生成' }).click()
  await expect(page).toHaveURL(/\/tasks\/new$/)
  await expect(page.getByText('重新生成自 old', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '重新生成', exact: true }).click()
  await expect(page).toHaveURL(/\/tasks\/new2$/)
  await expect(page.getByRole('heading', { name: '第二次生成完成', exact: true })).toBeVisible({ timeout: 10000 })
  await expect(page.getByText('重新生成自 new', { exact: true })).toBeVisible()
  expect(state.mutations).toHaveLength(3)
  expect(state.unexpected).toEqual([])
})

test('只读记忆管理允许读取并禁用写操作', async ({ page }) => {
  const state = await setup(page, 'readonly')
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('button', { name: '删除记忆' }).first()).toBeDisabled()
  await expect(dialog.getByRole('button', { name: '重新生成' }).first()).toBeDisabled()
  expect(state.mutations).toEqual([])
  expect(state.unexpected).toEqual([])
})

test('记忆故障明确不可用，不冒充空库', async ({ page }) => {
  const state = await setup(page, 'admin', true)
  await expect(page.getByRole('dialog')).toContainText('不能据此认定记忆为空')
  await expect(page.getByRole('dialog').locator('.memory-table')).toHaveCount(0)
  expect(state.unexpected).toEqual([])
})
