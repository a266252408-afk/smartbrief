import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const root = new URL('../../docs/evaluation-datasets/v1/', import.meta.url)
const golden = JSON.parse(readFileSync(fileURLToPath(new URL('private/golden.json', root)), 'utf8')).cases
const collections = ['golden', 'regression', 'challenge']
const cases = collections.flatMap(collection => JSON.parse(readFileSync(fileURLToPath(new URL(`private/${collection}.json`, root)), 'utf8')).cases)
const labels = { protocol: '流程与故障', search: '网络搜索', report: '报告质量', robustness: '鲁棒性与恢复', end_to_end: '完整任务' }
test('有限实验保留首次故障和两组尝试，质量未知不冒充改进', async ({ page }) => {
  const rid = 'er_'+'f'.repeat(32), writes: unknown[] = []
  const baseline = { case_id: 'G002', check_status: 'fail', task_execution_status: 'paused', provider_calls: 2, calls: 2, trace: [{ kind: 'model_call', model_role: 'primary', status: 'failed', error_code: 'MODEL_CONNECTION' }, { kind: 'model_call', model_role: 'backup', status: 'failed', error_code: 'MODEL_CONNECTION' }] }
  const attempt = { case_id: 'G002', check_status: 'pass', task_execution_status: 'completed', provider_calls: 5, calls: 5, trace: [], report_quality: 'unknown' }
  await page.route('**/api/v*/**', async route => {
    const path = new URL(route.request().url()).pathname, env = (data: unknown) => ({ data, meta: { request_id: 'fixture' } })
    if (path === '/api/v2/auth/session') return route.fulfill({ json: env({ role: 'admin' }) })
    if (path.endsWith('/catalog')) return route.fulfill({ json: env({ version: 'sb-eval-data-1', cutoff: '2026-10-31', total_cases: 108, areas: [{ area: 'end_to_end', experiment_case_ids: ['G002'] }], cases: [], collections: {} }) })
    if (path === '/api/v2/evaluations') return route.fulfill({ json: env({ items: [], total: 0, page: 1, page_size: 20 }) })
    if (path.endsWith('/comparisons')) return route.fulfill({ json: env({ items: [] }) })
    if (path.endsWith('/runs') && route.request().method() === 'POST') { writes.push(route.request().postDataJSON()); return route.fulfill({ status: 202, json: env({ run_id: rid }) }) }
    if (path.endsWith('/runs')) return route.fulfill({ json: env({ items: [] }) })
    if (path.endsWith('/runs/' + rid)) return route.fulfill({ json: env({ run_id: rid, profile: 'bounded_experiment', execution_status: 'completed', report_quality: 'unknown', metadata: { dataset_hash: 'a'.repeat(64), experiment_progress: [{ case_id: 'G002', phase: 'baseline', attempt: 0, result: baseline }, ...['plain_retry', 'reflection'].map(phase => ({ case_id: 'G002', phase, attempt: 1, result: attempt }))] }, summary: { planned: 1, processed: 1, passed: 1, failed: 0, unknown: 0 }, results: [{ case_id: 'G002', check_status: 'pass', baseline, arms: ['plain_retry', 'reflection'].map(mode => ({ mode, provider_calls: 5, ever_passed: true, always_passed: false, attempts: [attempt] })), error: null }] }) })
    return route.fulfill({ status: 404, json: { error: { message: '未声明请求' } } })
  })
  await page.goto('/evaluation/compare')
  await page.getByLabel('访问令牌').fill('fixture-experiment')
  await page.getByRole('button', { name: '验证并进入' }).click()
  await page.getByRole('button', { name: '运行有限重试与诊断对照', exact: true }).click()
  await expect(page.getByRole('heading', { name: '有限重试实验详情' })).toBeVisible()
  await expect(page.getByText('初始故障：未通过；供应商尝试 2', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '普通重试', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '公开诊断反馈', exact: true })).toBeVisible()
  await expect(page.getByRole('cell', { name: '待独立评判', exact: true })).toHaveCount(2)
  await page.getByRole('button', { name: '查看首次失败', exact: true }).click()
  await expect(page.getByText('主模型调用未成功', { exact: false })).toBeVisible()
  expect(writes).toEqual([{ profile: 'bounded_experiment', case_ids: ['G002'], max_attempts: 2, call_limit: 10 }])
  await page.screenshot({ path: '../docs/verification/EVAL-NEXT-07-02-experiment.png', fullPage: true })
})
test('专项跨版本比较预算不同时只并排且未知质量不填零', async ({ page }) => {
  const baseline = 'er_'+'a'.repeat(32), current = 'er_'+'b'.repeat(32), saved: any[] = []
  await page.route('**/api/v*/**', async route => {
    const path = new URL(route.request().url()).pathname, env = (data: unknown) => ({ data, meta: { request_id: 'fixture' } })
    if (path === '/api/v2/auth/session') return route.fulfill({ json: env({ role: 'admin' }) })
    if (path.endsWith('/catalog')) return route.fulfill({ json: env({ version: 'sb-eval-data-1', cutoff: '2026-10-31', total_cases: 108, areas: [], cases: [], collections: {} }) })
    if (path === '/api/v2/evaluations') return route.fulfill({ json: env({ items: [], total: 0, page: 1, page_size: 20 }) })
    if (path.endsWith('/runs')) return route.fulfill({ json: env({ items: [baseline, current].map(run_id => ({ run_id, profile: 'full_task_local', metadata: { model_group: 'primary' } })) }) })
    if (path.endsWith('/comparisons') && route.request().method() === 'POST') {
      saved.push({ ...route.request().postDataJSON(), comparison_id: 'ec_fixture', attribution_allowed: false, reasons: ['预算不同，不能归因'], differences: ['source_hash', 'execution_budget'], case_changes: [], execution_pass_rate_delta: null, quality_pass_rate_delta: null, all_attempt_seconds_delta: null, interpretation: '仅固定资料软件协议差异' })
      return route.fulfill({ status: 201, json: env(saved[0]) })
    }
    if (path.endsWith('/comparisons')) return route.fulfill({ json: env({ items: saved }) })
    return route.fulfill({ status: 404, json: { error: { message: '未声明请求' } } })
  })
  await page.goto('/evaluation/compare')
  await page.getByLabel('访问令牌').fill('fixture-comparison')
  await page.getByRole('button', { name: '验证并进入' }).click()
  for (const [label, id] of [['专项基线运行', baseline], ['专项当前运行', current]]) {
    const input = page.getByRole('combobox', { name: label, exact: true })
    await input.focus(); await input.press('Enter')
    await page.getByRole('listbox', { name: label, exact: true }).getByRole('option', { name: id+' primary', exact: true }).click()
  }
  await page.getByRole('button', { name: '保存专项对照', exact: true }).click()
  await expect(page.getByRole('heading', { name: '仅并排，不可归因', exact: true })).toBeVisible()
  await expect(page.getByText('预算不同，不能归因', { exact: true })).toBeVisible()
  await expect(page.getByTestId('comparison-result')).toContainText('独立质量差值：尚未独立评分')
  expect(saved[0]).toMatchObject({ baseline_id: baseline, current_id: current, changed_dimension: 'code' })
  await page.screenshot({ path: '../docs/verification/EVAL-NEXT-07-01-compare.png', fullPage: true })
})
test('完整任务选择接管组，完成和独立质量分开展示', async ({ page }) => {
  const rid = 'er_' + 'e'.repeat(32), writes: unknown[] = []
  await page.route('**/api/v*/**', async route => {
    const path = new URL(route.request().url()).pathname, env = (data: unknown) => ({ data, meta: { request_id: 'fixture' } })
    if (path === '/api/v2/auth/session') return route.fulfill({ json: env({ role: 'admin' }) })
    if (path.endsWith('/catalog')) return route.fulfill({ json: env({ version: 'sb-eval-data-1', cutoff: '2026-10-31', total_cases: 108, cases: [], collections: {}, areas: [{ area: 'end_to_end', label: '完整任务', profile: 'full_task_local', supported_case_ids: ['G017'], runnable: true, score: null }] }) })
    if (path.endsWith('/runs') && route.request().method() === 'POST') { writes.push(route.request().postDataJSON()); return route.fulfill({ status: 202, json: env({ run_id: rid }) }) }
    if (path.endsWith('/runs/' + rid)) return route.fulfill({ json: env({ run_id: rid, profile: 'full_task_local', execution_status: 'completed', report_quality: 'unknown', metadata: { dataset_hash: 'a'.repeat(64) }, summary: { planned: 1, processed: 1, passed: 1, failed: 0, unknown: 0 }, results: [{ case_id: 'G017', check_status: 'pass', task_execution_status: 'completed', business_quality: 'passed', report_quality: 'unknown', report: '固定资料简报 [S1]', calls: 6, trace: [{ kind: 'model_call', model_role: 'primary', status: 'failed', error_code: 'MODEL_CONNECTION' }, { kind: 'model_call', model_role: 'backup', status: 'success' }], evidence: [] }] }) })
    return route.fulfill({ status: 404, json: { error: { message: '未声明请求' } } })
  })
  await page.goto('/evaluation/run')
  await page.getByLabel('访问令牌').fill('fixture-full-task')
  await page.getByRole('button', { name: '验证并进入' }).click()
  await page.getByRole('combobox', { name: '模型运行组', exact: true }).focus()
  await page.getByRole('combobox', { name: '模型运行组', exact: true }).press('Enter')
  await page.getByRole('option', { name: '主故障后接管', exact: true }).click()
  await page.getByRole('button', { name: '运行完整任务替身基线', exact: true }).click()
  await expect(page.getByRole('heading', { name: '完整任务运行详情' })).toBeVisible()
  await expect(page.getByRole('cell', { name: '待独立评判', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '查看完整任务过程', exact: true }).click()
  await expect(page.getByText('备用模型调用成功', { exact: false })).toBeVisible()
  expect(writes).toEqual([{ profile: 'full_task_local', case_ids: ['G017'], group: 'mixed' }])
  await page.screenshot({ path: '../docs/verification/EVAL-NEXT-06-full-task.png', fullPage: true })
})
test('独立评分展示拒绝依据与校准未知，报告HTML不执行', async ({ page }) => {
  const rid = 'er_' + 'd'.repeat(32)
  const writes: unknown[] = []
  const record = { run_id: rid, profile: 'report_grading', execution_status: 'completed', report_quality: 'rejected', metadata: { dataset_hash: 'a'.repeat(64) }, summary: { planned: 1, processed: 1, passed: 0, failed: 1, unknown: 0 }, results: [{ case_id: 'G002', check_status: 'fail', report: '<img src=x onerror=alert(1)> 待补充', report_sha256: 'b'.repeat(64), judgements: [{ rubric_id: 'Q_CITE', label: 'fail', reason: '含有占位符' }] }] }
  await page.route('**/api/v*/**', async route => {
    const path = new URL(route.request().url()).pathname
    const env = (data: unknown) => ({ data, meta: { request_id: 'fixture' } })
    if (path === '/api/v2/auth/session') return route.fulfill({ json: env({ role: 'admin' }) })
    if (path === '/api/v2/evaluation-center/catalog') return route.fulfill({ json: env({ version: 'sb-eval-data-1', cutoff: '2026-10-31', total_cases: 108, cases: [], collections: {}, areas: [{ area: 'report', label: '报告质量', profile: 'report_grading', supported_case_ids: ['G002'], runnable: true, score: null }] }) })
    if (path.endsWith('/calibrate')) return route.fulfill({ json: env({ total: 10, unknown: 10, false_accepts: 0, false_rejects: 0, accuracy: null, groups: {}, results: [] }) })
    if (path.endsWith('/grade-report')) { writes.push(route.request().postDataJSON()); return route.fulfill({ status: 201, json: env(record) }) }
    if (path.endsWith('/runs/' + rid)) return route.fulfill({ json: env(record) })
    return route.fulfill({ status: 404, json: { error: { message: '未声明请求' } } })
  })
  await page.goto('/evaluation/run')
  await page.getByLabel('访问令牌').fill('fixture-grade')
  await page.getByRole('button', { name: '验证并进入' }).click()
  await page.getByRole('button', { name: '运行评分器校准', exact: true }).click()
  await expect(page.getByText('准确率 待真实校准', { exact: false })).toBeVisible()
  await page.getByLabel('待评报告').fill('<img src=x onerror=alert(1)> 待补充')
  await page.getByRole('button', { name: '独立评分并归档', exact: true }).click()
  await expect(page.getByRole('heading', { name: '独立报告评分详情' })).toBeVisible()
  await expect(page.getByRole('cell', { name: '含有占位符', exact: true })).toBeVisible()
  await expect(page.locator('img')).toHaveCount(0)
  expect(writes).toEqual([{ profile: 'report_grading', case_id: 'G002', report: '<img src=x onerror=alert(1)> 待补充' }])
  await page.screenshot({ path: '../docs/verification/EVAL-NEXT-05-report.png', fullPage: true })
})
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
