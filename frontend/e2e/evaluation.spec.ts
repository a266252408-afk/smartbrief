import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

// 浏览器使用明确HTTP夹具；20例真实后端结果单独集成验证。
const source = JSON.parse(readFileSync(fileURLToPath(new URL('../../docs/verification/T09-offline-report.json', import.meta.url)), 'utf8'))

for (const role of ['admin', 'readonly'] as const) {
  test(`评测离线标识、未知值、单变量对照和${role}权限`, async ({ page }) => {
    const unexpected: string[] = []
    const writes: any[] = []
    const current = { ...source, eval_id: 'current', metadata: { ...source.metadata, model: '<img src=x onerror=alert(1)>' } }
    const baseline = { ...source, eval_id: 'baseline' }
    await page.route('**/*', async route => {
      if (new URL(route.request().url()).origin === 'http://127.0.0.1:5175') await route.continue()
      else { unexpected.push(route.request().url()); await route.abort() }
    })
    await page.route('**/api/v*/**', async route => {
      const request = route.request()
      const url = new URL(request.url())
      const path = url.pathname
      const envelope = (data: any) => ({ data, meta: { request_id: 'fixture' } })
      if (path === '/api/v2/auth/session') return route.fulfill({ json: envelope({ role }) })
      if (path === '/api/v2/evaluations' && request.method() === 'POST') {
        writes.push(request.postDataJSON())
        return route.fulfill({ status: 202, json: envelope({ eval_id: 'current', status: 'running', total_cases: 20 }) })
      }
      if (path === '/api/v2/evaluations') return route.fulfill({ json: envelope({ items: [current, baseline], total: 2, page: 1, page_size: 20 }) })
      if (path === '/api/v2/evaluations/current/comparison') return route.fulfill({ status: 409, json: { error: { message: '源码不同，不能比较' } } })
      if (path === '/api/v2/evaluations/current') return route.fulfill({ json: envelope(current) })
      if (path === '/api/v2/evaluations/baseline') return route.fulfill({ json: envelope(baseline) })
      unexpected.push(path)
      return route.fulfill({ status: 500, json: { detail: '未声明请求' } })
    })
    await page.goto('/evaluation/protocol')
    await page.getByLabel('访问令牌').fill('fixture-evaluation-role')
    await page.getByRole('button', { name: '验证并进入' }).click()
    const panel = page.getByTestId('evaluation-protocol')
    await expect(panel).toContainText('不代表在线效果')
    await expect(panel).toContainText('输入 Token 未知')
    await expect(panel).toContainText('费用未知或部分已知')
    await expect(panel.locator('img')).toHaveCount(0)
    await expect(panel).toContainText('<img src=x')
    await panel.getByText('选择同配置的基线评测', { exact: true }).click()
    await page.getByRole('option', { name: 'baseline', exact: true }).click()
    await panel.getByRole('button', { name: '比较单个开关' }).click()
    await expect(page.getByTestId('compare-error')).toHaveText('源码不同，不能比较')
    const run = page.getByRole('button', { name: '运行离线协议评测' })
    if (role === 'readonly') {
      await expect(run).toBeDisabled()
      await expect(page.getByRole('checkbox', { name: '审核', exact: true })).toBeDisabled()
    } else {
      await page.getByText('审核', { exact: true }).click()
      await expect(page.getByRole('checkbox', { name: '审核', exact: true })).not.toBeChecked()
      await run.click()
      await expect.poll(() => writes.length).toBe(1)
      expect(writes[0]).toEqual({ suite_version: 'protocol-1', mode: 'offline', feature_flags: { rag: true, memory: true, review: false } })
    }
    if (role === 'readonly') expect(writes).toEqual([])
    await expect(page.locator('.radar-chart canvas')).toBeVisible()
    await page.screenshot({ path: `../docs/verification/EVAL-NEXT-01-protocol-${role}.png`, fullPage: true })
    expect(unexpected).toEqual([])
  })
}

test('评测历史错误不显示空库，旧记录保持版本未知', async ({ page }) => {
  let failed = true
  await page.route('**/api/v*/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v2/auth/session') return route.fulfill({ json: { data: { role: 'readonly' }, meta: { request_id: 'fixture' } } })
    if (path === '/api/v2/evaluations') {
      if (failed) return route.fulfill({ status: 503, json: { error: { message: '归档不可用' } } })
      return route.fulfill({ json: { data: { items: [{ ...source, eval_id: 'legacy', metadata: null }], total: 1, page: 1, page_size: 20 }, meta: { request_id: 'fixture' } } })
    }
    if (path === '/api/v2/evaluations/legacy') return route.fulfill({ json: { data: { ...source, eval_id: 'legacy', metadata: null }, meta: { request_id: 'fixture' } } })
    return route.fulfill({ status: 500, json: {} })
  })
  await page.goto('/evaluation/protocol')
  await page.getByLabel('访问令牌').fill('fixture-evaluation-error')
  await page.getByRole('button', { name: '验证并进入' }).click()
  await expect(page.getByText('评测历史读取失败，请刷新重试', { exact: true })).toBeVisible()
  await expect(page.getByText('暂无评测记录', { exact: true })).toHaveCount(0)
  failed = false
  await page.getByRole('link', { name: '任务提交', exact: true }).click()
  await page.getByRole('link', { name: '评测中心', exact: true }).click()
  await page.getByRole('link', { name: '原20例协议完整视图（兼容入口）', exact: true }).click()
  await expect(page.getByTestId('evaluation-protocol')).toContainText('版本信息未知')
})
