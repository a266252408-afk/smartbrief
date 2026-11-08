import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import RewriteAudit from '../RewriteAudit.vue'

describe('返工程序拦截诊断', () => {
  it('展示被拦截的提案、原因、范围和升级结果，安全呈现原文', () => {
    const wrapper = mount(RewriteAudit, { props: { audit: {
      mode: 'full', changed: true,
      program_restriction: { observed: true, outcome: 'candidate_applied_after_restriction',
        codes: ['SCOPE_RESTRICTED'], quality_effect: 'unknown_until_review' },
      attempts: [{ mode: 'patch', decision: 'applied',
        request: { scope: 'named_paragraphs', allowed_paragraphs: [2], missing_evidence_ids: ['S9'] },
        rejected: [{ index: 0, code: 'SCOPE_RESTRICTED', message: '补丁必须指向被点名段落',
          proposal: { replacement: '<script>提案</script>' } }],
        raw_response: '<img src=x onerror=alert(1)>', raw_truncated: true }],
    } } })
    expect(wrapper.text()).toContain('升级或重试后已应用候选稿')
    expect(wrapper.text()).toContain('程序是否拦截过提案：是')
    expect(wrapper.text()).toContain('SCOPE_RESTRICTED')
    expect(wrapper.text()).toContain('段落 2')
    expect(wrapper.text()).toContain('S9')
    expect(wrapper.text()).toContain('原文已截断')
    expect(wrapper.findAll('script,img')).toHaveLength(0)
  })
  it('旧记录不推断没有程序限制', () => {
    const wrapper = mount(RewriteAudit, { props: { audit: { mode: 'patch', changed: false } } })
    expect(wrapper.text()).toContain('无法判断程序是否限制了修改')
  })
  it('区分模型自报与实际验证，不把候选应用当作质量通过', () => {
    const wrapper = mount(RewriteAudit, { props: { audit: { mode: 'full', changed: true,
      verification: [
        { finding_id: 'BIND-1', reported_status: 'fixed', status: 'unresolved', reason: '数字仍在' },
        { finding_id: 'MODEL-2', reported_status: 'fixed', status: 'unverified', reason: '等待语义复审' },
      ],
    } } })
    expect(wrapper.text()).toContain('BIND-1：模型自报已修复；实际检查仍未解决')
    expect(wrapper.text()).toContain('MODEL-2：模型自报已修复；实际检查待独立复审')
    expect(wrapper.text()).toContain('整体质量仍以独立复审为准')
  })
})
