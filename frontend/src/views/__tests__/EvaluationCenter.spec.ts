import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import EvaluationCenter from '../EvaluationCenter.vue'
import { startLiveSearch, startExperiment, createComparison, fetchComparisons, startFullTask, gradeReport, calibrateGrader, fetchCatalog, fetchCatalogCase, startMechanism, startSearchReplay, fetchMechanismRuns, saveEvaluationReview, fetchEvaluationReviews } from '../../api/evaluationCenter'
import { fetchEvalReports, runEvaluation } from '../../api/evaluation'
import { authRole } from '../../api/session'

vi.mock('../../api/evaluationCenter', () => ({ startLiveSearch: vi.fn(), startExperiment: vi.fn(), createComparison: vi.fn(), fetchComparisons: vi.fn(), startFullTask: vi.fn(), gradeReport: vi.fn(), calibrateGrader: vi.fn(), fetchCatalog: vi.fn(), fetchCatalogCase: vi.fn(), startMechanism: vi.fn(), startSearchReplay: vi.fn(), fetchMechanismRuns: vi.fn(), fetchMechanismRun: vi.fn(), saveEvaluationReview: vi.fn(), fetchEvaluationReviews: vi.fn() }))
vi.mock('../../api/evaluation', () => ({ fetchEvalReports: vi.fn(), runEvaluation: vi.fn() }))
const pack = { version: 'sb-eval-data-1', cutoff: '2026-10-31', total_cases: 108, dataset_hash: 'a'.repeat(64), collections: { golden: 24 },
  areas: [{ area: 'end_to_end', label: '完整任务', case_count: 4, runnable: true, profile: 'full_task_local', availability: '完整图', score: null, supported_case_ids: ['G017'], experiment_case_ids: ['G002'] }, { area: 'report', label: '报告质量', case_count: 8, runnable: true, profile: 'report_grading', availability: '独立评分', score: null, supported_case_ids: ['G002'] }, { area: 'protocol', label: '流程与故障', case_count: 31, runnable: true, profile: 'protocol-1', availability: '原20例合成协议可运行', score: null },
    { area: 'robustness', label: '鲁棒性与恢复', case_count: 36, runnable: true, profile: 'mechanism_regression', availability: '本地机制回归已接通', score: null, supported_case_ids: ['R009', 'G023'] },
    { area: 'search', label: '网络搜索', case_count: 16, runnable: true, profile: 'search_replay', availability: '冻结响应回放', score: null, supported_case_ids: ['G001'], live_case_ids: ['G001'] }],
  cases: [{ case_id: 'G002', collection: 'golden', area: 'report', title: '腕戴研究', split: 'development', human_review: 'pending', run_status: 'not_run', source_count: 1, has_fixture: false }] }
let wrapper: ReturnType<typeof mount> | undefined
afterEach(() => { wrapper?.unmount(); vi.resetAllMocks() })
async function open(tab: string) {
  vi.mocked(fetchCatalog).mockResolvedValue(pack)
  vi.mocked(fetchEvalReports).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })
  vi.mocked(fetchMechanismRuns).mockResolvedValue([])
  vi.mocked(fetchComparisons).mockResolvedValue([])
  vi.mocked(fetchEvaluationReviews).mockResolvedValue([])
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: EvaluationCenter, meta: { evaluationTab: tab } },
    { path: '/evaluation/protocol', component: { template: '<p>协议结果</p>' } },
    { path: '/evaluation/runs/:runId', component: { template: '<p>专项结果</p>' } }
  ] })
  await router.push('/'); await router.isReady()
  wrapper = mount(EvaluationCenter, { global: { plugins: [ElementPlus, router] } })
  await flushPromises()
  return router
}
it('总览区分未测与可用能力，显示六个工作入口', async () => {
  await open('overview')
  expect(wrapper!.findAll('nav a')).toHaveLength(6)
  expect(wrapper!.text()).toContain('108 例')
  expect(wrapper!.text()).toContain('尚无本板块实测成绩')
  expect(wrapper!.text()).toContain('本地机制回归已接通')
})
it('案例详情以文本展示来源与检查项，不执行HTML', async () => {
  vi.mocked(fetchCatalogCase).mockResolvedValue({ case: { case_id: 'G002', title: '腕戴研究', input: { user_request: '<img src=x onerror=alert(1)>' }, oracle: { expected_handling: 'deliver_supported_answer', assertions: [{ check_id: 'A', rubric_id: 'Q_SCOPE', critical: true, requirement: '不得混淆地区' }] } }, sources: [], rubrics: [], fixture: null })
  await open('datasets')
  const button = wrapper!.findAll('button').find(b => b.text() === '查看详情')!
  await button.trigger('click'); await flushPromises()
  expect(wrapper!.get('[data-testid="catalog-detail"]').text()).toContain('不得混淆地区')
  expect(wrapper!.findAll('img')).toHaveLength(0)
  expect(wrapper!.text()).toContain('<img src=x')
})
it('只读不能启动；管理员启动原协议后进入具体记录', async () => {
  authRole.value = 'readonly'
  await open('run')
  const button = wrapper!.findAll('button').find(b => b.text() === '运行离线协议评测')!
  expect(button.attributes('disabled')).toBeDefined()
  expect(runEvaluation).not.toHaveBeenCalled()
  authRole.value = 'admin'; await flushPromises()
  vi.mocked(runEvaluation).mockResolvedValue({ eval_id: 'chosen', status: 'running', total_cases: 20 })
  await button.trigger('click'); await flushPromises()
  expect(runEvaluation).toHaveBeenCalledWith({ rag: true, memory: true, review: true })
})
it('读取失败显示错误，不能显示空题库成功', async () => {
  await open('overview')
  vi.mocked(fetchCatalog).mockRejectedValue(new Error('broken'))
  await wrapper!.get('nav').findAll('a')[0]!.trigger('click')
  // 重新挂载验证真实初始失败路径。
  wrapper!.unmount()
  vi.mocked(fetchCatalog).mockRejectedValue(new Error('broken'))
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: EvaluationCenter }] })
  await router.push('/'); await router.isReady()
  wrapper = mount(EvaluationCenter, { global: { plugins: [ElementPlus, router] } }); await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('题库读取失败')
  expect(wrapper.findAll('.areas')).toHaveLength(0)
})
it('机制回归只读禁写，管理员提交案例且保持质量边界', async () => {
  authRole.value = 'readonly'
  const router = await open('run')
  const button = wrapper!.findAll('button').find(b => b.text() === '运行主备与恢复回归')!
  expect(button.attributes('disabled')).toBeDefined()
  expect(startMechanism).not.toHaveBeenCalled()
  expect(wrapper!.text()).toContain('不证明线上备用可用或报告质量')
  authRole.value = 'admin'; await flushPromises()
  vi.mocked(startMechanism).mockResolvedValue({ run_id: 'er_' + 'a'.repeat(32), execution_status: 'running', report_quality: 'unknown', created_at: 'fixture', error: null, results: [], summary: { planned: 2, processed: 0, passed: 0, failed: 0, unknown: 0 } })
  await button.trigger('click'); await flushPromises()
  expect(startMechanism).toHaveBeenCalledWith(['R009', 'G023'])
  expect(router.currentRoute.value.path).toContain('/evaluation/runs/er_')
})
it('人工核验默认无法确认，显式提交且不批量更改其他题', async () => {
  authRole.value = 'admin'
  vi.mocked(fetchCatalogCase).mockResolvedValue({ case: { case_id: 'G002', title: '腕戴研究', input: { user_request: '核对腕戴' }, oracle: { expected_handling: 'deliver_supported_answer', assertions: [] } }, sources: [], rubrics: [], fixture: null })
  await open('datasets')
  await wrapper!.findAll('button').find(b => b.text() === '查看详情')!.trigger('click'); await flushPromises()
  const save = wrapper!.findAll('button').find(b => b.text() === '保存题目核验')!
  expect(save.attributes('disabled')).toBeDefined()
  await wrapper!.get('textarea').setValue('发现需要进一步核对的口径')
  vi.mocked(saveEvaluationReview).mockResolvedValue({ review_id: 'fixture', target: 'case_definition', case_id: 'G002', run_id: null, dataset_hash: 'a'.repeat(64), decision: 'unknown', notes: '核对中', actor: 'admin', recorded_at: 'fixture', supersedes: null })
  await save.trigger('click'); await flushPromises()
  expect(saveEvaluationReview).toHaveBeenCalledWith({ target: 'case_definition', case_id: 'G002', run_id: null, dataset_hash: 'a'.repeat(64), decision: 'unknown', notes: '发现需要进一步核对的口径' })
  expect(wrapper!.get('[role="status"]').text()).toContain('原始机器成绩和题库内容未改写')
})
it('搜索回放明确未联网，只有管理员可以提交', async () => {
  authRole.value = 'readonly'
  const router = await open('run')
  const button = wrapper!.findAll('button').find(b => b.text() === '运行搜索回放')!
  expect(button.attributes('disabled')).toBeDefined()
  expect(startSearchReplay).not.toHaveBeenCalled()
  expect(wrapper!.text()).toContain('不调用真实搜索或模型')
  authRole.value = 'admin'; await flushPromises()
  vi.mocked(startSearchReplay).mockResolvedValue({ run_id: 'er_' + 'b'.repeat(32), profile: 'search_replay', execution_status: 'running', report_quality: 'unknown', created_at: 'fixture', error: null, results: [], summary: { planned: 1, processed: 0, passed: 0, failed: 0, unknown: 0 } })
  await button.trigger('click'); await flushPromises()
  expect(startSearchReplay).toHaveBeenCalledWith(['G001'])
  expect(router.currentRoute.value.path).toContain('/evaluation/runs/er_')
})

it('独立报告评分只读禁写，校准未知不显示准确率零', async () => {
  authRole.value = 'readonly'; await open('run')
  const button = wrapper!.findAll('button').find(b => b.text() === '独立评分并归档')!
  expect(button.attributes('disabled')).toBeDefined()
  expect(gradeReport).not.toHaveBeenCalled()
  authRole.value = 'admin'; await flushPromises()
  vi.mocked(calibrateGrader).mockResolvedValue({ total: 10, unknown: 10, false_accepts: 0, false_rejects: 0, accuracy: null, groups: {}, results: [] })
  await wrapper!.findAll('button').find(b => b.text() === '运行评分器校准')!.trigger('click'); await flushPromises()
  expect(wrapper!.text()).toContain('待真实校准')
})

it('完整任务入口只读禁写且管理员明确提交替身配置', async () => {
  authRole.value = 'readonly'; const router = await open('run')
  const button = wrapper!.findAll('button').find(b => b.text() === '运行完整任务替身基线')!
  expect(button.attributes('disabled')).toBeDefined(); expect(startFullTask).not.toHaveBeenCalled()
  authRole.value = 'admin'; await flushPromises()
  vi.mocked(startFullTask).mockResolvedValue({ run_id: 'er_' + 'e'.repeat(32) } as any)
  await button.trigger('click'); await flushPromises()
  expect(startFullTask).toHaveBeenCalledWith(['G017'], 'primary')
  expect(router.currentRoute.value.path).toContain('/evaluation/runs/er_')
})

it('专项对照只读可查看不可创建，未知质量不显示零', async () => {
  authRole.value = 'readonly'
  const result = { comparison_id: 'ec_fixture', baseline_id: 'a', current_id: 'b', changed_dimension: 'code', attribution_allowed: false, reasons: ['预算不同'], differences: ['execution_budget'], case_changes: [], execution_pass_rate_delta: null, quality_pass_rate_delta: null, all_attempt_seconds_delta: null, interpretation: '仅协议结果' } as any
  vi.mocked(fetchComparisons).mockResolvedValue([result])
  const router = await open('compare')
  // open默认空归档；再次加载路由后使用显式记录响应。
  vi.mocked(fetchComparisons).mockResolvedValue([result])
  await router.push('/?reload=1'); await flushPromises()
  const save = wrapper!.findAll('button').find(b => b.text() === '保存专项对照')!
  expect(save.attributes('disabled')).toBeDefined(); expect(createComparison).not.toHaveBeenCalled()
  await wrapper!.findAll('button').find(b => b.text() === '查看专项对照')!.trigger('click'); await flushPromises()
  expect(wrapper!.text()).toContain('预算不同'); expect(wrapper!.text()).toContain('尚未独立评分')
})

it('有限实验只读禁写，管理员提交有上限的开发组实验', async () => {
  authRole.value = 'readonly'; const router = await open('compare')
  const button = wrapper!.findAll('button').find(b => b.text() === '运行有限重试与诊断对照')!
  expect(button.attributes('disabled')).toBeDefined(); expect(startExperiment).not.toHaveBeenCalled()
  authRole.value = 'admin'; await flushPromises()
  vi.mocked(startExperiment).mockResolvedValue({ run_id: 'er_' + 'f'.repeat(32) } as any)
  await button.trigger('click'); await flushPromises()
  expect(startExperiment).toHaveBeenCalledWith(['G002'], 2, 10)
  expect(router.currentRoute.value.path).toContain('/evaluation/runs/er_')
})

it('真实搜索默认无授权不能运行，编号只用于服务端核验', async () => {
  authRole.value = 'admin'; const router = await open('run')
  const button = wrapper!.findAll('button').find(b => b.text() === '按已批准授权运行真实搜索')!
  expect(button.attributes('disabled')).toBeDefined(); expect(startLiveSearch).not.toHaveBeenCalled()
  await wrapper!.get('input[aria-label="真实搜索单次授权编号"]').setValue('approved-server-id')
  vi.mocked(startLiveSearch).mockResolvedValue({ run_id: 'er_' + '9'.repeat(32) } as any)
  await button.trigger('click'); await flushPromises()
  expect(startLiveSearch).toHaveBeenCalledWith(['G001'], 'approved-server-id')
  expect(router.currentRoute.value.path).toContain('/evaluation/runs/er_')
})

it('详情立即显示加载，失败可重试，关闭后迟到响应不重新打开', async () => {
  let resolve!: (value: any) => void
  vi.mocked(fetchCatalogCase).mockRejectedValueOnce(new Error('网络异常'))
  await open('datasets')
  const button = wrapper!.findAll('button').find(b => b.text() === '查看详情')!
  await button.trigger('click'); await flushPromises()
  expect(wrapper!.get('[role="dialog"]').attributes('aria-label')).toBe('案例详情')
  expect(wrapper!.text()).toContain('案例详情读取失败')
  vi.mocked(fetchCatalogCase).mockImplementationOnce(() => new Promise(r => { resolve = r }))
  await wrapper!.findAll('button').find(b => b.text() === '重试详情')!.trigger('click')
  expect(wrapper!.text()).toContain('正在读取案例')
  await wrapper!.findAll('button').find(b => b.text() === '关闭详情')!.trigger('click')
  resolve({ case: { case_id: 'G002', title: '迟到详情', input: { user_request: '迟到响应' } }, sources: [], rubrics: [], fixture: null })
  await flushPromises()
  expect(wrapper!.find('[data-testid="catalog-detail"]').exists()).toBe(false)
})
