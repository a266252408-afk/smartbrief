import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const root = new URL('../../docs/evaluation-datasets/v1/', import.meta.url)
const golden = JSON.parse(readFileSync(fileURLToPath(new URL('private/golden.json', root)), 'utf8')).cases
const collections = ['golden', 'regression', 'challenge']
const cases = collections.flatMap(collection => JSON.parse(readFileSync(fileURLToPath(new URL(`private/${collection}.json`, root)), 'utf8')).cases)
const labels = { protocol: '流程与故障', search: '网络搜索', report: '报告质量', robustness: '鲁棒性与恢复', end_to_end: '完整任务' }
test('搜索回放展示采集指标和资料，语义未知不显示零分', async ({ page }) => {
  const rid = 'er_' + 'c'.repeat(32)
  const writes: unknown[] = []
  await page.route('**/api/v*/**', async route => {
    const path = new URL(route.request().url()).pathname
    const env = (data: unknown) => ({ data, meta: { request_id: 'fixture' } })
    if (path === '/api/v2/auth/session') return route.fulfill({ json: env({ role: 'admin' }) })
    if (path === '/api/v2/evaluation-center/catalog') return route.fulfill({ json: env({ version: 'sb-eval-data-1', cutoff: '2026-10-31', total_cases: 108, cases: [], collections: {}, areas: [{ area: 'search', label: '网络搜索', case_count: 16, runnable: true, profile: 'search_replay', supported_case_ids: ['G001'], availability: '冻结回放已接通', score: null }] }) })
    if (path === '/api/v2/evaluation-center/runs' && route.request().method() === 'POST') {
      writes.push(route.request().postDataJSON()); return route.fulfill({ status: 202, json: env({ run_id: rid, profile: 'search_replay' }) })
    }
    if (path === '/api/v2/evaluation-center/runs/' + rid) return route.fulfill({ json: env({ run_id: rid, profile: 'search_replay', execution_status: 'completed', report_quality: 'unknown', metadata: { dataset_hash: 'a'.repeat(64) }, summary: { planned: 1, processed: 1, passed: 0, failed: 0, unknown: 1 }, error: null, results: [{ case_id: 'G001', check_status: 'unknown', metrics: { returned_count: 1, unique_sources: 1, unknown_period_documents: 0, stale_documents: null, relevance_at_5: null }, trace: [{ kind: 'search_request', query: '全球腕戴设备', provider: 'frozen_replay' }], evidence: [{ title: '冻结资料卡', url: 'https://example.invalid/frozen', snippet: '冻结参考片段，并非当次联网结果。', published_at: '2026-09-09', data_period: '2026Q2', content_available: true }] }] }) })
    return route.fulfill({ status: 404, json: { error: { message: '未声明请求' } } })
  })
  await page.goto('/evaluation/run')
  await page.getByLabel('访问令牌').fill('fixture-search-replay')
  await page.getByRole('button', { name: '验证并进入' }).click()
  await page.getByRole('button', { name: '运行搜索回放', exact: true }).click()
  await expect(page.getByRole('heading', { name: '冻结搜索回放详情' })).toBeVisible()
  await expect(page.getByRole('cell', { name: '待独立评判', exact: true })).toBeVisible()
  await expect(page.getByRole('cell', { name: '未指定窗口', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '查看回放资料', exact: true }).click()
  await expect(page.getByText('回放查询：全球腕戴设备（未联网）', { exact: true })).toBeVisible()
  await expect(page.getByText('冻结参考片段，并非当次联网结果。', { exact: true })).toBeVisible()
  expect(writes).toEqual([{ profile: 'search_replay', case_ids: ['G001'] }])
  await page.screenshot({ path: '../docs/verification/EVAL-NEXT-04-search.png', fullPage: true })
})
test('人工只核验所选题目，结论和备注可追溯且HTML不执行', async ({ page }) => {
  const reviews: any[] = []
  await page.route('**/api/v*/**', async route => {
    const path = new URL(route.request().url()).pathname
    const env = (data: unknown) => ({ data, meta: { request_id: 'fixture' } })
    if (path === '/api/v2/auth/session') return route.fulfill({ json: env({ role: 'admin' }) })
    if (path === '/api/v2/evaluation-center/catalog') return route.fulfill({ json: env({ version: 'sb-eval-data-1', cutoff: '2026-10-31', total_cases: 108, dataset_hash: 'a'.repeat(64), collections: {}, areas: [], cases: cases.map(c => ({ ...c, human_review: c.case_id === 'G002' && reviews.length ? 'pass' : 'pending' })) }) })
    if (path === '/api/v2/evaluation-center/cases/G002') return route.fulfill({ json: env({ case: golden[1], sources: [JSON.parse(readFileSync(fileURLToPath(new URL('sources/S01.json', root)), 'utf8'))], rubrics: [], fixture: null }) })
    if (path === '/api/v2/evaluation-center/reviews' && route.request().method() === 'POST') {
      const data = { ...route.request().postDataJSON(), review_id: 'evr_fixture', actor: 'admin', recorded_at: '2026-10-31T00:00:00Z', supersedes: null }
      reviews.push(data); return route.fulfill({ status: 201, json: env(data) })
    }
    if (path === '/api/v2/evaluation-center/reviews') return route.fulfill({ json: env({ items: reviews }) })
    return route.fulfill({ status: 404, json: { error: { message: '未声明请求' } } })
  })
  await page.goto('/evaluation/datasets')
  await page.getByLabel('访问令牌').fill('fixture-review')
  await page.getByRole('button', { name: '验证并进入' }).click()
  await page.getByLabel('搜索案例').fill('G002')
  await page.getByRole('button', { name: '查看详情', exact: true }).click()
  await page.getByRole('combobox', { name: '题目核验结论', exact: true }).focus()
  await page.getByRole('combobox', { name: '题目核验结论', exact: true }).press('Enter')
  await page.getByRole('option', { name: '核验通过', exact: true }).click()
  await page.getByLabel('核验备注').fill('<img src=x onerror=alert(1)> 已核对口径')
  await page.getByRole('button', { name: '保存题目核验', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('人工核验已保存')
  expect(reviews).toHaveLength(1)
  expect(reviews[0]).toMatchObject({ target: 'case_definition', case_id: 'G002', decision: 'pass', dataset_hash: 'a'.repeat(64), run_id: null })
  await page.getByRole('link', { name: '人工复核', exact: true }).click()
  await expect(page.getByRole('cell', { name: '<img src=x onerror=alert(1)> 已核对口径', exact: true })).toBeVisible()
  await expect(page.locator('img')).toHaveCount(0)
  await page.screenshot({ path: '../docs/verification/EVAL-NEXT-03-review.png', fullPage: true })
})
test('管理员启动机制回归并查看真实判定与质量未知', async ({ page }) => {
  const rid = 'er_' + 'a'.repeat(32)
  const writes: unknown[] = []
  await page.route('**/api/v*/**', async route => {
    const path = new URL(route.request().url()).pathname
    const env = (data: unknown) => ({ data, meta: { request_id: 'fixture' } })
    if (path === '/api/v2/auth/session') return route.fulfill({ json: env({ role: 'admin' }) })
    if (path === '/api/v2/evaluation-center/catalog') return route.fulfill({ json: env({ version: 'sb-eval-data-1', cutoff: '2026-10-31', total_cases: 108, cases: [], collections: {}, areas: [{ area: 'robustness', label: '鲁棒性与恢复', case_count: 36, runnable: true, profile: 'mechanism_regression', supported_case_ids: ['R009', 'G023'], availability: '已接通机制回归', score: null }] }) })
    if (path === '/api/v2/evaluation-center/runs' && route.request().method() === 'POST') {
      writes.push(route.request().postDataJSON())
      return route.fulfill({ status: 202, json: env({ run_id: rid }) })
    }
    if (path === '/api/v2/evaluation-center/runs/' + rid) return route.fulfill({ json: env({ run_id: rid, execution_status: 'completed', report_quality: 'unknown', summary: { planned: 2, processed: 2, passed: 1, failed: 0, unknown: 1 }, error: null, results: [{ case_id: 'R009', check_status: 'pass', test_count: 1, skipped: 0, duration_seconds: 1, error: null, trace: [{ kind: 'model_call', model_role: 'primary', status: 'failed', error_code: 'MODEL_CONNECTION', duration_ms: 1 }, { kind: 'model_call', model_role: 'backup', status: 'success', error_code: null, duration_ms: 2 }] }, { case_id: 'G023', check_status: 'unknown', test_count: 0, skipped: 0, duration_seconds: 1, error: '子进程未运行' }] }) })
    return route.fulfill({ status: 404, json: { error: { message: '未声明请求' } } })
  })
  await page.goto('/evaluation/run')
  await page.getByLabel('访问令牌').fill('fixture-mechanism')
  await page.getByRole('button', { name: '验证并进入' }).click()
  await page.getByRole('button', { name: '运行主备与恢复回归', exact: true }).click()
  await expect(page.getByText('已处理 2/2', { exact: false })).toBeVisible()
  await expect(page.getByText('报告质量尚未评判', { exact: false })).toBeVisible()
  await expect(page.getByRole('cell', { name: '待确认', exact: true })).toBeVisible()
  expect(writes).toEqual([{ profile: 'mechanism_regression', case_ids: ['R009', 'G023'] }])
  await page.getByRole('row').filter({ hasText: 'R009' }).getByRole('button', { name: '查看过程' }).click()
  await expect(page.getByText('主模型调用未成功', { exact: false })).toBeVisible()
  await expect(page.getByText('备用模型调用成功', { exact: false })).toBeVisible()
  await page.screenshot({ path: '../docs/verification/EVAL-NEXT-02-02-trace.png', fullPage: true })
})
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
  await expect(page.getByRole('button', { name: '保存题目核验', exact: true })).toBeDisabled()
  await page.screenshot({ path: '../docs/verification/EVAL-NEXT-01-catalog.png', fullPage: true })
  await page.getByRole('link', { name: '运行评测', exact: true }).click()
  await expect(page.getByRole('button', { name: '运行离线协议评测', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '执行器待接通' })).toHaveCount(4)
  expect(writes).toEqual([])
})
