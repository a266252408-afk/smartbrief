import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ReviewEvidence from '../ReviewEvidence.vue'
import RewriteAudit from '../RewriteAudit.vue'
import { roundOutcome, stopExplanation } from '../../utils/reviewDiagnostics'
import type { ReviewRound } from '../../api/tasks'

describe('用户可理解的修复诊断', () => {
  it('区分确认、疑点、已驳回与历史未知，不把soft事实疑点当文风', () => {
    const round: ReviewRound = { round: 1, passed: false, comments: [], quality_status: 'unavailable',
      stop_reason: 'BUDGET_EXCEEDED', review_diagnostics: { code: 'REVIEW_JSON_INVALID', message: '内部格式故障' },
      findings: [
        { finding_id: 'A', decision: 'confirmed', severity: 'hard', material: true, problem: '年份冲突' },
        { finding_id: 'B', decision: 'unresolved', severity: 'soft', material: true, problem: '排序待核对' },
        { finding_id: 'C', decision: 'dismissed', severity: 'hard', material: false, problem: '已驳回旧意见' },
        { finding_id: 'D', severity: 'hard', problem: '旧记录未判定' },
      ] }
    const view = mount(ReviewEvidence, { props: { round } })
    const summary = view.get('[data-testid="review-summary"]')
    expect(summary.text()).toContain('已确认问题 1 项')
    expect(summary.text()).toContain('待核验疑点 1 项')
    expect(summary.text()).toContain('历史问题 1 项')
    expect(summary.text()).toContain('审核响应不可用')
    expect(summary.text()).toContain('保留已有报告')
    expect(summary.text()).not.toContain('BUDGET_EXCEEDED')
    const technical = view.get('[data-testid="review-technical"]')
    expect(technical.attributes('open')).toBeUndefined()
    expect(technical.text()).toContain('BUDGET_EXCEEDED')
  })
  it('明确质量不可用或审核不完整时，旧passed=true也不得展示通过', () => {
    const base: ReviewRound = { round: 1, passed: true, comments: [], quality_status: 'unavailable', review_complete: false }
    expect(roundOutcome(base, 'unavailable').type).not.toBe('success')
    expect(roundOutcome({ ...base, quality_status: 'passed' }, 'passed').type).not.toBe('success')
    expect(roundOutcome({ ...base, quality_status: 'passed', review_complete: true }, 'passed').type).toBe('success')
    expect(roundOutcome({ round: 1, passed: true, comments: [] }, 'unknown').label).toContain('未知')
    expect(roundOutcome({ round: 1, passed: true, comments: [] }, 'unavailable').type).not.toBe('success')
    expect(stopExplanation('UNKNOWN_INTERNAL_CODE')).not.toContain('UNKNOWN_INTERNAL_CODE')
  })
  it('争议核验完成和实际修改均不冒充整稿质量通过，原文安全呈现', () => {
    const view = mount(ReviewEvidence, { props: { round: { round: 1, passed: false, comments: [],
      dispute_review: { status: 'reviewed', initial_findings: ['<img src=x onerror=alert(1)>'] } } } })
    expect(view.text()).toContain('整稿质量仍以审核结果为准')
    expect(view.findAll('img,script')).toHaveLength(0)
    const audit = mount(RewriteAudit, { props: { audit: { mode: 'deterministic_dedup', changed: true,
      review_required: true, attempts: [], failures: [] } } })
    expect(audit.find('summary').text()).toContain('程序去重')
    expect(audit.text()).toContain('仍需整稿复审')
  })
})
