import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import TaskControls from '../TaskControls.vue'
import { cancelTask, resumeTask } from '../../api/tasks'
import { authRole } from '../../api/session'

vi.mock('../../api/tasks', () => ({ cancelTask: vi.fn(), resumeTask: vi.fn() }))
let wrapper: ReturnType<typeof mount>
beforeEach(() => {
  authRole.value = 'admin'
  vi.mocked(cancelTask).mockResolvedValue({ task_id: 'queued', status: 'cancelled', execution_status: 'cancelled', cancelled: true, already_cancelled: false })
  vi.mocked(resumeTask).mockResolvedValue({ task_id: 'paused', status: 'queued', execution_status: 'queued', resumed: true, resume_mode: 'restart' })
})
afterEach(() => { wrapper?.unmount(); vi.clearAllMocks(); authRole.value = null })

it('排队任务可取消', () => {
  wrapper = mount(TaskControls, { props: { taskId: 'queued', status: 'queued' } })
  expect(wrapper.text()).toContain('取消任务')
})

it('离线任务禁止普通恢复，运行中仍能取消', async () => {
  wrapper = mount(TaskControls, { props: { taskId: 'offline', status: 'failed', offline: true } })
  expect(wrapper.find('button').exists()).toBe(false)
  expect(wrapper.text()).toContain('评测中心重新运行')
  await wrapper.setProps({ status: 'running' })
  await wrapper.get('button').trigger('click'); await flushPromises()
  expect(cancelTask).toHaveBeenCalledWith('offline')
  expect(resumeTask).not.toHaveBeenCalled()
})

it('只读不能取消或恢复，终态不提供恢复', async () => {
  authRole.value = 'readonly'
  wrapper = mount(TaskControls, { props: { taskId: 'queued', status: 'queued' } })
  expect(wrapper.get('button').attributes('disabled')).toBeDefined()
  await wrapper.get('button').trigger('click')
  expect(cancelTask).not.toHaveBeenCalled()
  await wrapper.setProps({ status: 'paused' })
  expect(wrapper.get('button').attributes('disabled')).toBeDefined()
  await wrapper.setProps({ status: 'cancelled' })
  expect(wrapper.find('button').exists()).toBe(false)
})

it('取消后刷新状态，错误显示且可以重试', async () => {
  vi.mocked(cancelTask).mockRejectedValueOnce({ response: { data: { error: { message: '状态写入失败' } } } })
  wrapper = mount(TaskControls, { props: { taskId: 'queued', status: 'queued' } })
  await wrapper.get('button').trigger('click'); await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toBe('状态写入失败')
  expect(wrapper.emitted('changed')).toBeUndefined()
  await wrapper.get('button').trigger('click'); await flushPromises()
  expect(wrapper.text()).toContain('任务已取消')
  expect(wrapper.emitted('changed')).toHaveLength(1)
})

it.each(['restart', 'checkpoint'] as const)('恢复明确展示%s而不假称一定续跑', async mode => {
  vi.mocked(resumeTask).mockResolvedValue({ task_id: 'paused', status: 'queued', execution_status: 'queued', resumed: true, resume_mode: mode })
  wrapper = mount(TaskControls, { props: { taskId: 'paused', status: 'failed' } })
  await wrapper.get('button').trigger('click'); await flushPromises()
  expect(wrapper.text()).toContain(mode === 'restart' ? '从头重试' : '断点续跑')
  expect(wrapper.emitted('changed')).toHaveLength(1)
})

it('重复恢复明确已经在队列中', async () => {
  vi.mocked(resumeTask).mockResolvedValue({ task_id: 'paused', status: 'queued', execution_status: 'queued', resumed: false, resume_mode: null })
  wrapper = mount(TaskControls, { props: { taskId: 'paused', status: 'paused' } })
  await wrapper.get('button').trigger('click'); await flushPromises()
  expect(wrapper.text()).toContain('任务已在排队或执行')
})

it('操作期间禁重入，切换任务后丢弃旧响应', async () => {
  let resolve!: (value: Awaited<ReturnType<typeof cancelTask>>) => void
  vi.mocked(cancelTask).mockImplementationOnce(() => new Promise(r => { resolve = r }))
  wrapper = mount(TaskControls, { props: { taskId: 'queued', status: 'running' } })
  await wrapper.get('button').trigger('click')
  await wrapper.get('button').trigger('click')
  expect(cancelTask).toHaveBeenCalledTimes(1)
  await wrapper.setProps({ taskId: 'new', status: 'queued' })
  resolve({ task_id: 'queued', status: 'cancelled', execution_status: 'cancelled', cancelled: true, already_cancelled: false })
  await flushPromises()
  expect(wrapper.text()).not.toContain('任务已取消')
  expect(wrapper.emitted('changed')).toBeUndefined()
})
