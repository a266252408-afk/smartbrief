import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import ElementPlus, { ElSelect } from 'element-plus'
import EvaluationProtocol from '../EvaluationProtocol.vue'
import { compareEvaluations, type EvalReport } from '../../api/evaluation'

vi.mock('../../api/evaluation', () => ({ compareEvaluations: vi.fn() }))
let wrapper: ReturnType<typeof mount>
const report = {
  eval_id: 'current', status: 'completed',
  metadata: { mode: 'offline', suite_version: 'protocol-1', dataset_hash: 'a'.repeat(64), source_hash: 'b'.repeat(64), code_commit: 'commit', code_tag: 'tag', code_dirty: true, model: '<img src=x onerror=alert(1)>', prompt_version: 'fixture', feature_flags: { rag: true, memory: false, review: true }, memory_protocol: '冷启动', randomness: '固定响应' },
  metrics: { quality: { quality_pass_rate: 0.75, execution_completion_rate: 0.85, pending_cases: 0, citation_valid_rate: null, input_tokens: null, output_tokens: null, p95_completed_seconds: 2, estimated_costs: {}, cost_complete: false, failure_categories: { MODEL_TIMEOUT: 1 } } }
} as unknown as EvalReport
const props = { report, reports: [{ eval_id: 'baseline', status: 'completed' }] as any }
afterEach(() => { wrapper?.unmount(); vi.clearAllMocks() })

it('合成/人工pending与未知用量费用明确显示，模型文本不执行', () => {
  wrapper = mount(EvaluationProtocol, { props, global: { plugins: [ElementPlus] } })
  expect(wrapper.text()).toContain('不代表在线效果')
  expect(wrapper.text()).toContain('pending')
  expect(wrapper.text()).toContain('未知（无引用）')
  expect(wrapper.text()).toContain('输入 Token 未知')
  expect(wrapper.text()).toContain('费用未知或部分已知')
  expect(wrapper.find('img').exists()).toBe(false)
  expect(wrapper.text()).toContain('<img src=x')
})

it('旧结果不补造版本，也不提供对照', () => {
  wrapper = mount(EvaluationProtocol, { props: { ...props, report: { ...report, metadata: null } }, global: { plugins: [ElementPlus] } })
  expect(wrapper.text()).toContain('版本信息未知')
  expect(wrapper.find('button').exists()).toBe(false)
})

it('比较失败展示原因，重试结果只称协议差值', async () => {
  vi.mocked(compareEvaluations).mockRejectedValueOnce({ response: { data: { error: { message: '不同源码不可比较' } } } })
    .mockResolvedValueOnce({ changed_feature: 'memory', quality_pass_rate_delta: 0, execution_completion_rate_delta: 0, interpretation: '协议差值，不能证明在线提升' })
  wrapper = mount(EvaluationProtocol, { props, global: { plugins: [ElementPlus] } })
  wrapper.findComponent(ElSelect).vm.$emit('update:modelValue', 'baseline')
  await flushPromises()
  await wrapper.get('[data-testid="compare-evaluations"]').trigger('click'); await flushPromises()
  expect(compareEvaluations).toHaveBeenCalledWith('current', 'baseline')
  expect(wrapper.get('[data-testid="compare-error"]').text()).toBe('不同源码不可比较')
  await wrapper.get('[data-testid="compare-evaluations"]').trigger('click'); await flushPromises()
  expect(wrapper.text()).toContain('不能证明在线提升')
})

it('切换报告后拒收旧对照响应', async () => {
  let resolve!: (value: any) => void
  vi.mocked(compareEvaluations).mockImplementationOnce(() => new Promise(done => { resolve = done }))
  wrapper = mount(EvaluationProtocol, { props, global: { plugins: [ElementPlus] } })
  wrapper.findComponent(ElSelect).vm.$emit('update:modelValue', 'baseline')
  await flushPromises()
  await wrapper.get('[data-testid="compare-evaluations"]').trigger('click')
  await wrapper.setProps({ report: { ...report, eval_id: 'other' } })
  resolve({ changed_feature: 'memory', quality_pass_rate_delta: 1, interpretation: '迟到的旧结论' })
  await flushPromises()
  expect(wrapper.text()).not.toContain('迟到的旧结论')
})
