import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const root = new URL('../../docs/evaluation-datasets/v1/', import.meta.url)
const golden = JSON.parse(readFileSync(fileURLToPath(new URL('private/golden.json', root)), 'utf8')).cases
const collections = ['golden', 'regression', 'challenge']
const cases = collections.flatMap(collection => JSON.parse(readFileSync(fileURLToPath(new URL(`private/${collection}.json`, root)), 'utf8')).cases)
const labels = { protocol: '流程与故障', search: '网络搜索', report: '报告质量', robustness: '鲁棒性与恢复', end_to_end: '完整任务' }
test('题库分区、筛选、详情和只读运行边界', async ({ page }) => {
  const writes: string[] = []
  await page.route('**/api/v*/**', async route => {
    const path = new URL(route.request().url()).pathname
    const envelope = (data: unknown) => ({ data, meta: { request_id: 'fixture' } })
    if (route.request().method() !== 'GET') writes.push(path)
    if (path === '/api/v2/auth/session') return route.fulfill({ json: envelope({ role: 'readonly' }) })
    if (path === '/api/v2/evaluation-center/catalog') return route.fulfill({ json: envelope({ version: 'sb-eval-data-1', cutoff: '2026-10-31', total_cases: 108, dataset_hash: 'a'.repeat(64), collections: { golden: 24, regression: 24, challenge: 40, legacy: 20 },
      cases: cases.map(c => ({ ...c, human_review: 'pending' })), areas: Object.entries(labels).map(([area, label]) => ({ area, label, case_count: cases.filter(c => c.area === area).length, runnable: area === 'protocol', profile: area === 'protocol' ? 'protocol-1' : null, availability: area === 'protocol' ? '原20例合成协议可运行' : '题目已准备，执行器待接通', score: null })) }) })
    if (path === '/api/v2/evaluations') return route.fulfill({ json: envelope({ items: [], total: 0, page: 1, page_size: 20 }) })
    if (path === '/api/v2/evaluation-center/cases/G002') return route.fulfill({ json: envelope({ case: golden[1], sources: [JSON.parse(readFileSync(fileURLToPath(new URL('sources/S01.json', root)), 'utf8'))], rubrics: [], fixture: null }) })
    return route.fulfill({ status: 404, json: { error: { message: '未声明请求' } } })
  })
  await page.goto('/evaluation')
  await page.getByLabel('访问令牌').fill('fixture-eval-center')
  await page.getByRole('button', { name: '验证并进入' }).click()
  await expect(page.getByTestId('evaluation-center')).toContainText('108 例')
  await expect(page.getByText('尚无本板块实测成绩')).toHaveCount(5)
  await page.getByRole('link', { name: '测试集与标准', exact: true }).click()
  await page.getByLabel('搜索案例').fill('G002')
  await expect(page.getByText('筛选结果 1 例')).toBeVisible()
  await page.getByRole('button', { name: '查看详情', exact: true }).click()
  await expect(page.getByTestId('catalog-detail')).toContainText('3708')
  await page.screenshot({ path: '../docs/verification/EVAL-NEXT-01-catalog.png', fullPage: true })
  await page.getByRole('link', { name: '运行评测', exact: true }).click()
  await expect(page.getByRole('button', { name: '运行离线协议评测', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '执行器待接通' })).toHaveCount(4)
  expect(writes).toEqual([])
})
