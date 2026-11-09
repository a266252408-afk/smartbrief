import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import TaskDetail from '../TaskDetail.vue'
import { fetchTaskStatus, type TaskStatus } from '../../api/tasks'
vi.mock('vue-router', () => ({ useRoute: () => ({ params: { taskId: 't' } }), useRouter: () => ({ push: vi.fn() }) }))
vi.mock('html2pdf.js', () => ({ default: vi.fn() }))
vi.mock('../../api/tasks', () => ({ fetchTaskStatus: vi.fn(), resumeTask: vi.fn() }))
let wrapper: VueWrapper | undefined
afterEach(() => { wrapper?.unmount(); vi.restoreAllMocks() })
describe('任务详情以明确质量状态展示结果', () => {
  it.each([
    [undefined, false, '质量未知'], ['unknown', false, '质量未知'], ['rejected', true, '未通过（强制输出）'],
    ['unavailable', false, '审核不可用'], ['not_run', false, '未审核'],
    ['unavailable', true, '审核不可用'], ['unknown', true, '质量未知'], ['not_run', true, '未审核']
  ] as const)('%s 不因旧passed=true显示审核通过', async (quality, forced, label) => {
    vi.mocked(fetchTaskStatus).mockResolvedValue({
      task_id: 't', topic: '历史报告', status: 'completed', execution_status: 'completed',
      quality_status: quality, forced_output: forced, error_code: null, passed: true,
      forced_pass: forced, review_history: quality === undefined || quality === 'unknown' ? [{ round: 1, passed: true, comments: [] }] : [], review_comments: [], iteration: 0, max_iterations: 3,
      plan: [], completed_steps: [], react_logs: [], memory_hits: [], draft_content: '报告',
      final_report: '报告', research_data: {}, current_step_index: 0, error: null,
      created_at: '', updated_at: ''
    } as TaskStatus)
    wrapper = mount(TaskDetail, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.get('[data-testid="quality-status"]').text()).toBe(label)
    expect(wrapper.text()).not.toContain('审核通过')
    expect(wrapper.text()).not.toContain('强制通过')
    expect(wrapper.findAll('[data-testid="quality-status"].el-tag--success')).toHaveLength(0)
  })
})
