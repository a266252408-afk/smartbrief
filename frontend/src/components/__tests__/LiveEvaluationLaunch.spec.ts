import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import LiveEvaluationLaunch from '../LiveEvaluationLaunch.vue'
import { fetchLiveConfig } from '../../api/evaluationCenter'
import { authRole } from '../../api/session'
vi.mock('../../api/evaluationCenter', () => ({ fetchLiveConfig: vi.fn() }))
const config = { dataset_hash: 'a'.repeat(64), model: { name: 'current-model', base_url: 'https://model.invalid/v1', ready: true }, search: { ready: true, base_url: 'https://api.exa.ai/search' }, judge: { ready: true, name: 'judge-model', base_url: 'https://judge.invalid/v1', same_as_primary: false, source: 'dedicated' as const }, scope: '公开资料' }
let wrapper: ReturnType<typeof mount>
afterEach(() => { wrapper?.unmount(); vi.resetAllMocks() })
async function open(kind: 'model' | 'search' | 'judge' = 'model') {
  vi.mocked(fetchLiveConfig).mockResolvedValue(config)
  wrapper = mount(LiveEvaluationLaunch, { props: { kind, caseCount: 1 }, global: { plugins: [ElementPlus] } })
  await flushPromises()
}
it('显示当前模型与去向，未填写价格/确认时不启动', async () => {
  authRole.value = 'admin'; await open()
  expect(wrapper.text()).toContain('current-model')
  expect(wrapper.text()).toContain('https://model.invalid/v1')
  expect(wrapper.findAll('button').find(b => b.text() === '确认运行真实模型评测')!.attributes('disabled')).toBeDefined()
  expect(wrapper.emitted('launch')).toBeUndefined()
})
it('配置失败可重新读取，只读不能提交', async () => {
  authRole.value = 'readonly'; await open()
  expect(wrapper.get('fieldset').attributes('disabled')).toBeDefined()
  vi.mocked(fetchLiveConfig).mockRejectedValueOnce(new Error('unavailable'))
  await wrapper.findAll('button').find(b => b.text() === '刷新配置')!.trigger('click'); await flushPromises()
  expect(wrapper.text()).toContain('配置读取失败')
})
it('真实搜索必须确认预算才提交，预算改变撤销勾选', async () => {
  authRole.value = 'admin'; await open('search')
  await wrapper.get('input[aria-label="每请求估计费用上限"]').setValue('0.01')
  await wrapper.get('input[aria-label="本次估计费用上限"]').setValue('0.1')
  await wrapper.get('input[type="checkbox"]').setValue(true)
  await wrapper.findAll('button').find(b => b.text() === '确认运行真实搜索评测')!.trigger('click')
  expect(wrapper.emitted('launch')?.[0]?.[0]).toMatchObject({ confirmed: true, dataset_hash: 'a'.repeat(64), per_call_estimate_ceiling: 0.01, estimated_cost_ceiling: 0.1 })
  await wrapper.get('input[aria-label="本次估计费用上限"]').setValue('0.2')
  expect((wrapper.get('input[type="checkbox"]').element as HTMLInputElement).checked).toBe(false)
})

it('独立评分显示评分模型并提交独立调用预算', async () => {
  authRole.value = 'admin'; await open('judge')
  expect(wrapper.text()).toContain('judge-model')
  expect(wrapper.text()).toContain('本次评分总调用上限')
  await wrapper.get('input[aria-label="每百万输入Token单价"]').setValue('1')
  await wrapper.get('input[aria-label="每百万输出Token单价"]').setValue('2')
  await wrapper.get('input[aria-label="本次估计费用上限"]').setValue('2')
  await wrapper.get('input[type="checkbox"]').setValue(true)
  await wrapper.findAll('button').find(b => b.text() === '确认运行真实独立评分')!.trigger('click')
  expect(wrapper.emitted('launch')?.[0]?.[0]).toMatchObject({ model: 'judge-model', base_url: 'https://judge.invalid/v1', confirmed: true })
})
