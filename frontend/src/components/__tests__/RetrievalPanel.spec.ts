import { expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import RetrievalPanel from '../RetrievalPanel.vue'
import type { RetrievalRecord } from '../../api/tasks'

const base: RetrievalRecord = { task_id: 't', document_ids: ['a'.repeat(32)], query: '项目资料',
  options: { top_k: 5, threshold: 0.35, context_budget: 6000 }, status: 'matched', error_code: null,
  evidence: [{ source_id: 'S1', task_id: 't', kind: 'document', document_id: 'a'.repeat(32), chunk_id: 'chunk',
    title: '<script>恶意标题</script>', excerpt: '<img src=x onerror=alert(1)>137台设备', url: null,
    retrieved_at: '2026-10-20T00:00:00Z', source_deleted: true, page: 2, score: 0.9 }] }

it('展示页码、片段、相似度与删除提示，外部资料只能作为文本', () => {
  const wrapper = mount(RetrievalPanel, { props: { records: [base] }, global: { plugins: [ElementPlus] } })
  expect(wrapper.text()).toContain('来源已删除')
  expect(wrapper.text()).toContain('页码 2')
  expect(wrapper.text()).toContain('0.9000')
  expect(wrapper.text()).toContain('137台设备')
  expect(wrapper.find('img, script').exists()).toBe(false)
  wrapper.unmount()
})

it.each([
  ['no_hits', '检索无命中'], ['below_threshold', '相关性不足'], ['budget_excluded', '超出上下文预算'],
  ['documents_missing', '已删除或不存在'], ['embedding_failed', '嵌入服务失败'], ['index_failed', '文档索引失败']
] as const)('区分%s，不伪装成已使用资料', (status, text) => {
  const wrapper = mount(RetrievalPanel, { props: { records: [{ ...base, status, evidence: [] }] }, global: { plugins: [ElementPlus] } })
  expect(wrapper.text()).toContain(text)
  expect(wrapper.findAll('.retrieval-hit')).toHaveLength(0)
  wrapper.unmount()
})
