import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import EvaluationCenter from '../EvaluationCenter.vue'
import { fetchCatalog, fetchCatalogCase } from '../../api/evaluationCenter'
import { fetchEvalReports, runEvaluation } from '../../api/evaluation'
import { authRole } from '../../api/session'

vi.mock('../../api/evaluationCenter', () => ({ fetchCatalog: vi.fn(), fetchCatalogCase: vi.fn() }))
vi.mock('../../api/evaluation', () => ({ fetchEvalReports: vi.fn(), runEvaluation: vi.fn() }))
const pack = { version: 'sb-eval-data-1', cutoff: '2026-10-31', total_cases: 108, dataset_hash: 'a'.repeat(64), collections: { golden: 24 },
  areas: [{ area: 'protocol', label: '流程与故障', case_count: 31, runnable: true, profile: 'protocol-1', availability: '原20例合成协议可运行', score: null },
    { area: 'robustness', label: '鲁棒性与恢复', case_count: 36, runnable: false, profile: null, availability: '题目已准备，执行器待接通', score: null }],
  cases: [{ case_id: 'G002', collection: 'golden', area: 'report', title: '腕戴研究', split: 'development', human_review: 'pending', run_status: 'not_run', source_count: 1, has_fixture: false }] }
let wrapper: ReturnType<typeof mount> | undefined
afterEach(() => { wrapper?.unmount(); vi.resetAllMocks() })
async function open(tab: string) {
  vi.mocked(fetchCatalog).mockResolvedValue(pack)
  vi.mocked(fetchEvalReports).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: EvaluationCenter, meta: { evaluationTab: tab } },
    { path: '/evaluation/protocol', component: { template: '<p>协议结果</p>' } }
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
  expect(wrapper!.text()).toContain('执行器待接通')
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
