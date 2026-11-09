import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ReviewEvidence from '../ReviewEvidence.vue'

describe('声明依据', () => {
  it('旧记录明确提示未记录', () => {
    const wrapper = mount(ReviewEvidence, { props: { round: { round: 1, passed: false, comments: [] } } })
    expect(wrapper.text()).toContain('未记录逐项事实绑定')
  })
  it('展示报告原句和完整来源原句', () => {
    const wrapper = mount(ReviewEvidence, { props: { round: {
      round: 1, passed: false, comments: [],
      claim_bindings: [{ paragraph: 2, text: '第一季度规模10亿元[S1]', fact_ids: ['F1'], source_ids: ['S1'] }],
      fact_registry: { F1: { fact_id: 'F1', task_id: 't', source_id: 'S1',
        quote: '2026年第一季度规模10亿元。', source: {} as never, measurements: [] } },
      dispute_review: { status: 'unresolved', initial_findings: [] },
    } } })
    expect(wrapper.text()).toContain('2026年第一季度规模10亿元。')
    expect(wrapper.text()).toContain('待核验，质量尚未确认')
  })
})
