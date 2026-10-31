import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import EvaluationCenter from '../EvaluationCenter.vue'
import { fetchCatalog, fetchCatalogCase, startMechanism, fetchMechanismRuns, saveEvaluationReview, fetchEvaluationReviews } from '../../api/evaluationCenter'
import { fetchEvalReports, runEvaluation } from '../../api/evaluation'
import { authRole } from '../../api/session'

vi.mock('../../api/evaluationCenter', () => ({ fetchCatalog: vi.fn(), fetchCatalogCase: vi.fn(), startMechanism: vi.fn(), fetchMechanismRuns: vi.fn(), fetchMechanismRun: vi.fn(), saveEvaluationReview: vi.fn(), fetchEvaluationReviews: vi.fn() }))
vi.mock('../../api/evaluation', () => ({ fetchEvalReports: vi.fn(), runEvaluation: vi.fn() }))
const pack = { version: 'sb-eval-data-1', cutoff: '2026-10-31', total_cases: 108, dataset_hash: 'a'.repeat(64), collections: { golden: 24 },
  areas: [{ area: 'protocol', label: '流程与故障', case_count: 31, runnable: true, profile: 'protocol-1', availability: '原20例合成协议可运行', score: null },
    { area: 'robustness', label: '鲁棒性与恢复', case_count: 36, runnable: true, profile: 'mechanism_regression', availability: '本地机制回归已接通', score: null, supported_case_ids: ['R009', 'G023'] }],
  cases: [{ case_id: 'G002', collection: 'golden', area: 'report', title: '腕戴研究', split: 'development', human_review: 'pending', run_status: 'not_run', source_count: 1, has_fixture: false }] }
let wrapper: ReturnType<typeof mount> | undefined
afterEach(() => { wrapper?.unmount(); vi.resetAllMocks() })
async function open(tab: string) {
  vi.mocked(fetchCatalog).mockResolvedValue(pack)
  vi.mocked(fetchEvalReports).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })
  vi.mocked(fetchMechanismRuns).mockResolvedValue([])
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
