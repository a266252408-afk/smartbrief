import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import TaskDetail from '../TaskDetail.vue'
import { authRole } from '../../api/session'
import { fetchTaskStatus, resumeTask, type TaskStatus } from '../../api/tasks'

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { taskId: 'demo-service-recovery' } }),
  useRouter: () => ({ push: vi.fn() })
}))
vi.mock('html2pdf.js', () => ({ default: vi.fn() }))
vi.mock('../../api/tasks', () => ({
  fetchTaskStatus: vi.fn(),
  resumeTask: vi.fn(),
  cancelTask: vi.fn(),
  regenerateTask: vi.fn(),
  fetchTaskUsage: vi.fn()
}))

const base: TaskStatus = {
  task_id: 'demo-service-recovery', topic: '服务中断恢复演示（演示样例）',
  status: 'paused', execution_status: 'paused', quality_status: 'pending',
  forced_output: false, error_code: 'SERVICE_INTERRUPTED', error: '演示数据：服务中断后任务已暂停',
  plan: [{ id: 1, agent: 'researcher', task: '检索', depends_on: [] }],
  completed_steps: [1], current_step_index: 1, research_data: {}, draft_content: '', final_report: null,
  react_logs: [], review_comments: [], review_history: [], iteration: 0, max_iterations: 3,
  passed: false, forced_pass: false, memory_hits: [], created_at: '', updated_at: '',
  metadata: { demo: true, generator_version: 'demo-1.0', demo_sample_id: 'service-recovery', demo_sample_version: '1.0' }
}

let wrapper: VueWrapper
beforeEach(() => {
  authRole.value = 'admin'
  vi.mocked(fetchTaskStatus).mockResolvedValue(structuredClone(base))
})
afterEach(() => {
  wrapper?.unmount()
  authRole.value = null
  vi.restoreAllMocks()
})

describe('任务详情演示数据保护', () => {
  it.each([false, true])('离线任务隐藏恢复和重生成入口（旧记录=%s）', async legacy => {
    vi.mocked(fetchTaskStatus).mockResolvedValue({ ...structuredClone(base),
      task_id: 'offline', topic: legacy ? '[离线协议] 测试主题' : '测试主题',
      metadata: legacy ? {} : { execution_mode: 'offline', evaluation_case_id: 'C01' } })
    wrapper = mount(TaskDetail, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const labels = wrapper.findAll('button').map(button => button.text())
    expect(labels).not.toContain('恢复任务')
    expect(labels).not.toContain('重新生成')
    expect(wrapper.text()).toContain('评测中心重新运行')
  })
  it('展示演示标识与恢复说明，隐藏恢复/取消/重新生成入口', async () => {
    wrapper = mount(TaskDetail, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.get('[data-testid="demo-badge"]').text()).toBe('演示数据')
    const notice = wrapper.get('[data-testid="demo-notice"]').text()
    expect(notice).toContain('不会启动真实生成')
    expect(notice).toContain('服务中断')
    expect(notice).toContain('恢复任务')
    const labels = wrapper.findAll('button').map(button => button.text())
    expect(labels).not.toContain('恢复任务')
    expect(labels).not.toContain('取消任务')
    expect(labels).not.toContain('重新生成')
    expect(resumeTask).not.toHaveBeenCalled()
  })

  it('真实暂停任务仍保留恢复入口', async () => {
    vi.mocked(fetchTaskStatus).mockResolvedValue({ ...structuredClone(base), metadata: {},
      task_id: 'task_real', error: '服务中断', error_code: 'SERVICE_INTERRUPTED' })
    wrapper = mount(TaskDetail, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="demo-badge"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="demo-notice"]').exists()).toBe(false)
    expect(wrapper.findAll('button').map(button => button.text())).toContain('恢复任务')
  })
})
