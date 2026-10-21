import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import UsagePanel from '../UsagePanel.vue'
import { fetchTaskUsage, type UsageReport } from '../../api/tasks'

vi.mock('../../api/tasks', () => ({ fetchTaskUsage: vi.fn() }))
const sample: UsageReport = { task_id: 'old', items: [{ call_id: 'one', task_id: 'old', agent: 'writer',
  model: '<img src=x onerror=alert(1)>', status: 'failed', duration_ms: 10, input_tokens: null,
  output_tokens: null, usage_known: false, estimated_cost: null, currency: null, attempt: 2,
  reserved_tokens: 100, created_at: '2026-10-21T00:00:00Z', error_code: 'MODEL_TIMEOUT' }],
  call_count: 1, blocked_count: 0, unknown_calls: 1, input_tokens: null, output_tokens: null,
  duration_ms: 10, estimated_costs: {}, cost_complete: false, budget_limit_tokens: 20000,
  budget_charged_tokens: 100, budget_remaining_tokens: 19900, budget_estimated: true }
let wrapper: ReturnType<typeof mount>
beforeEach(() => { vi.mocked(fetchTaskUsage).mockResolvedValue(structuredClone(sample)) })
afterEach(() => { wrapper?.unmount(); vi.useRealTimers(); vi.restoreAllMocks(); vi.clearAllMocks() })
async function open() { await wrapper.findAll('button')[0].trigger('click'); await flushPromises() }

describe('模型用量面板', () => {
  it('按需展示用量与费用入口', () => {
    wrapper = mount(UsagePanel, { props: { taskId: 'old' } })
    expect(wrapper.text()).toContain('查看用量与费用')
    expect(fetchTaskUsage).not.toHaveBeenCalled()
  })
  it('未知Token与费用保持未知，固定错误码、尝试与模型文本安全展示', async () => {
    wrapper = mount(UsagePanel, { props: { taskId: 'old' } })
    await open()
    expect(fetchTaskUsage).toHaveBeenCalledWith('old')
    expect(wrapper.text()).toContain('输入 Token：未知')
    expect(wrapper.text()).toContain('费用未知')
    expect(wrapper.text()).toContain('含估算预留')
    expect(wrapper.text()).toContain('MODEL_TIMEOUT')
    expect(wrapper.find('img, script').exists()).toBe(false)
  })
  it('真实零Token/零费用不显示未知，不混淆不同币种与部分总额', async () => {
    vi.mocked(fetchTaskUsage).mockResolvedValue({ ...sample, input_tokens: 0, output_tokens: 0,
      estimated_costs: { USD: 0, CNY: 0.25 }, cost_complete: false })
    wrapper = mount(UsagePanel, { props: { taskId: 'old' } })
    await open()
    expect(wrapper.text()).toContain('输入 Token：0')
    expect(wrapper.text()).toContain('已知部分估算费用（非总额）')
    expect(wrapper.text()).toContain('USD 0.00000000')
    expect(wrapper.text()).toContain('CNY 0.25000000')
  })
  it('未完成调用显示调用中，不能伪装成中断失败', async () => {
    vi.mocked(fetchTaskUsage).mockResolvedValue({ ...sample, items: [{ ...sample.items[0]!, status: 'running', error_code: null }] })
    wrapper = mount(UsagePanel, { props: { taskId: 'old' } })
    await open()
    expect(wrapper.find('tbody').text()).toContain('调用中')
    expect(wrapper.find('tbody').text()).not.toContain('失败')
  })
  it('读取错误不冒充空账本且可重试', async () => {
    vi.mocked(fetchTaskUsage).mockRejectedValueOnce(new Error('fixture'))
    wrapper = mount(UsagePanel, { props: { taskId: 'old' } })
    await open()
    expect(wrapper.get('[role="alert"]').text()).toContain('暂不可用')
    expect(wrapper.text()).not.toContain('尚无调用记录')
    await open()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })
  it('历史空账本不能声称零Token或零费用', async () => {
    vi.mocked(fetchTaskUsage).mockResolvedValue({ ...sample, items: [], call_count: 0, unknown_calls: 0 })
    wrapper = mount(UsagePanel, { props: { taskId: 'old' } })
    await open()
    expect(wrapper.text()).toContain('历史用量未知')
    expect(wrapper.text()).toContain('输入 Token：未知')
  })
  it('切换任务拒绝旧ID迟到响应并清空旧数据', async () => {
    let resolve!: (report: UsageReport) => void
    vi.mocked(fetchTaskUsage).mockImplementationOnce(() => new Promise(r => { resolve = r }))
    wrapper = mount(UsagePanel, { props: { taskId: 'old' } })
    await wrapper.findAll('button')[0].trigger('click')
    await wrapper.setProps({ taskId: 'new' })
    resolve(sample); await flushPromises()
    expect(wrapper.text()).not.toContain('MODEL_TIMEOUT')
    await open()
    expect(fetchTaskUsage).toHaveBeenLastCalledWith('new')
  })
  it('按需开启运行任务轮询，终态和卸载后停止', async () => {
    vi.useFakeTimers()
    wrapper = mount(UsagePanel, { props: { taskId: 'old', running: true } })
    await open()
    await vi.advanceTimersByTimeAsync(2000); await flushPromises()
    expect(fetchTaskUsage).toHaveBeenCalledTimes(2)
    await wrapper.setProps({ running: false })
    await vi.advanceTimersByTimeAsync(4000)
    expect(fetchTaskUsage).toHaveBeenCalledTimes(2)
  })
  it('可导出读取到的JSON账本', async () => {
    vi.useFakeTimers()
    const create = vi.fn(() => 'blob:fixture'), revoke = vi.fn()
    vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: revoke })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    wrapper = mount(UsagePanel, { props: { taskId: 'old' } })
    await open()
    await wrapper.findAll('button')[1].trigger('click')
    expect(create).toHaveBeenCalledOnce()
    expect(click).toHaveBeenCalledOnce()
    await vi.runAllTimersAsync()
    expect(revoke).toHaveBeenCalledWith('blob:fixture')
    vi.unstubAllGlobals()
  })
})
