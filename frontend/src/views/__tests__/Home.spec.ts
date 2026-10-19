import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElMessage } from 'element-plus'
import Home from '../Home.vue'
import { submitTask } from '../../api/tasks'

const { push } = vi.hoisted(() => ({ push: vi.fn() }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))
vi.mock('../../api/tasks', () => ({ submitTask: vi.fn() }))

let wrapper: VueWrapper
beforeEach(() => {
  vi.spyOn(ElMessage, 'warning').mockImplementation(() => ({ close: vi.fn() }))
  vi.spyOn(ElMessage, 'success').mockImplementation(() => ({ close: vi.fn() }))
  vi.spyOn(ElMessage, 'error').mockImplementation(() => ({ close: vi.fn() }))
  wrapper = mount(Home, { global: { plugins: [ElementPlus] } })
})
afterEach(() => {
  wrapper.unmount()
  vi.restoreAllMocks()
})

function submit() { return wrapper.findAll('button')[0].trigger('click') }

describe('主题提交行为', () => {
  it('拒绝空白主题且不调用服务', async () => {
    await wrapper.get('textarea').setValue('   ')
    await submit()
    expect(ElMessage.warning).toHaveBeenCalledWith('请输入研究主题')
    expect(submitTask).not.toHaveBeenCalled()
    expect(push).not.toHaveBeenCalled()
  })

  it('提交裁剪后的主题并跳转到服务返回的任务', async () => {
    vi.mocked(submitTask).mockResolvedValue({ task_id: 'fixture-1', status: 'running', topic: '离线主题' })
    await wrapper.get('textarea').setValue('  离线主题  ')
    await submit()
    await flushPromises()
    expect(submitTask).toHaveBeenCalledWith('离线主题')
    expect(push).toHaveBeenCalledWith('/tasks/fixture-1')
  })

  it('展示服务错误并允许再次提交', async () => {
    vi.mocked(submitTask).mockRejectedValueOnce({ response: { data: { detail: '固定服务故障' } } })
    await wrapper.get('textarea').setValue('离线主题')
    await submit()
    await flushPromises()
    expect(ElMessage.error).toHaveBeenCalledWith('固定服务故障')
    expect(push).not.toHaveBeenCalled()
    vi.mocked(submitTask).mockResolvedValueOnce({ task_id: 'retry', status: 'running', topic: '离线主题' })
    await submit()
    await flushPromises()
    expect(submitTask).toHaveBeenCalledTimes(2)
    expect(push).toHaveBeenCalledWith('/tasks/retry')
  })

  it('请求未完成时阻止重复提交', async () => {
    let resolve!: (value: Awaited<ReturnType<typeof submitTask>>) => void
    vi.mocked(submitTask).mockReturnValueOnce(new Promise(done => { resolve = done }))
    await wrapper.get('textarea').setValue('离线主题')
    await submit()
    await wrapper.get('textarea').trigger('keydown', { key: 'Enter' })
    expect(submitTask).toHaveBeenCalledTimes(1)
    resolve({ task_id: 'once', status: 'running', topic: '离线主题' })
    await flushPromises()
    expect(push).toHaveBeenCalledWith('/tasks/once')
  })
})
