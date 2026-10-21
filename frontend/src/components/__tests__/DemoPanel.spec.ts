import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElMessage } from 'element-plus'
import DemoPanel from '../DemoPanel.vue'
import { authRole } from '../../api/session'
import { fetchDemoEnvironment, fetchDemoSamples, importDemoSamples, type DemoSample } from '../../api/demo'

const { push } = vi.hoisted(() => ({ push: vi.fn() }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))
vi.mock('../../api/demo', () => ({
  fetchDemoEnvironment: vi.fn(),
  fetchDemoSamples: vi.fn(),
  importDemoSamples: vi.fn()
}))

const samples: DemoSample[] = [
  { sample_id: 'normal-report', task_id: 'demo-normal-report', title: '正常报告样例', description: '正常流程说明',
    scenario: 'normal', sample_version: '1.0', imported: false },
  { sample_id: 'document-trace', task_id: 'demo-document-trace', title: '文档溯源样例', description: '文档流程说明',
    scenario: 'document', sample_version: '1.0', imported: false },
  { sample_id: 'service-recovery', task_id: 'demo-service-recovery', title: '服务恢复样例', description: '恢复流程说明',
    scenario: 'recovery', sample_version: '1.0', imported: false }
]

let wrapper: VueWrapper
beforeEach(() => {
  authRole.value = 'admin'
  vi.spyOn(ElMessage, 'success').mockImplementation(() => ({ close: vi.fn() }))
  vi.mocked(fetchDemoEnvironment).mockResolvedValue({
    demo_available: true, demo_count: 3, llm_configured: false, embedding_configured: false,
    execution_mode: 'offline', message: '未配置模型 Key：可导入演示样例查看完整页面，在线生成不可用。'
  })
  vi.mocked(fetchDemoSamples).mockResolvedValue(structuredClone(samples))
})

afterEach(() => {
  wrapper?.unmount()
  authRole.value = null
  vi.restoreAllMocks()
})

async function mountPanel() {
  wrapper = mount(DemoPanel, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  return wrapper
}

describe('演示流程入口', () => {
  it('展示离线环境提示与三条样例，不冒充在线生成', async () => {
    const view = await mountPanel()
    expect(view.get('[data-testid="demo-environment"]').text()).toContain('离线演示环境')
    expect(view.get('[data-testid="demo-environment"]').text()).toContain('未配置模型 Key')
    expect(view.text()).toContain('正常报告样例')
    expect(view.text()).toContain('文档溯源样例')
    expect(view.text()).toContain('服务恢复样例')
    expect(view.text()).toContain('正常报告')
    expect(view.text()).toContain('文档溯源')
    expect(view.text()).toContain('服务异常恢复')
    expect(view.text()).toContain('显式导入 · 不调用模型')
    expect(view.findAll('[data-sample]')).toHaveLength(3)
  })

  it('管理员导入全部后刷新样例，不调用真实生成', async () => {
    vi.mocked(importDemoSamples).mockResolvedValue({ imported: structuredClone(samples), skipped: [] })
    vi.mocked(fetchDemoSamples)
      .mockResolvedValueOnce(structuredClone(samples))
      .mockResolvedValueOnce(samples.map(sample => ({ ...sample, imported: true })))
    const view = await mountPanel()
    await view.findAll('button').find(button => button.text() === '导入全部演示')!.trigger('click')
    await flushPromises()
    expect(importDemoSamples).toHaveBeenCalledWith(undefined)
    expect(fetchDemoSamples).toHaveBeenCalledTimes(2)
    expect(view.findAll('[data-sample]').every(item => item.text().includes('已导入'))).toBe(true)
    expect(ElMessage.success).toHaveBeenCalledWith('已导入 3 个演示样例（不会启动生成）')
  })

  it('只读角色不能导入，只能查看管理员导入结果', async () => {
    authRole.value = 'readonly'
    const view = await mountPanel()
    expect(view.findAll('button').some(button => button.text() === '导入全部演示')).toBe(false)
    expect(view.findAll('button').filter(button => button.text() === '导入此样例').every(button => button.attributes('disabled') !== undefined)).toBe(true)
    expect(view.text()).toContain('管理员可导入')
  })

  it('已导入样例跳转到报告或任务详情', async () => {
    vi.mocked(fetchDemoSamples).mockResolvedValue(samples.map(sample => ({ ...sample, imported: true })))
    const view = await mountPanel()
    await view.get('[data-sample="normal-report"]').findAll('button').find(button => button.text() === '查看演示')!.trigger('click')
    await view.get('[data-sample="service-recovery"]').findAll('button').find(button => button.text() === '查看演示')!.trigger('click')
    expect(push).toHaveBeenNthCalledWith(1, '/report/demo-normal-report')
    expect(push).toHaveBeenNthCalledWith(2, '/tasks/demo-service-recovery')
  })

  it('导入失败显示明确错误，不提示成功', async () => {
    vi.mocked(importDemoSamples).mockRejectedValue({ response: { data: { error: { message: '演示样例导入失败，未改写用户数据' } } } })
    const view = await mountPanel()
    await view.findAll('button').find(button => button.text() === '导入全部演示')!.trigger('click')
    await flushPromises()
    expect(view.get('[role="alert"]').text()).toContain('未改写用户数据')
    expect(ElMessage.success).not.toHaveBeenCalled()
  })
})
