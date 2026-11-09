import type { ReviewRound, QualityStatus } from '../api/tasks'

type Outcome = { label: string; type: 'success' | 'warning' | 'danger' | 'info' }
export function roundOutcome(round: ReviewRound, quality?: QualityStatus): Outcome {
  if (quality === 'unknown' || (!quality && !round.quality_status)) return { label: '历史记录（质量未知）', type: 'info' }
  const resolved = round.quality_status || quality
  if (resolved === 'rejected') return { label: '未通过', type: 'danger' }
  if (resolved === 'unavailable' || round.passed === null) return { label: '审核不可用', type: 'warning' }
  if (resolved === 'not_run') return { label: '未审核', type: 'info' }
  if (round.review_complete === false) return { label: '审核不完整', type: 'warning' }
  if (resolved === 'pending') return { label: '待审核', type: 'warning' }
  if (round.passed === true && resolved === 'passed') return { label: '通过', type: 'success' }
  return { label: resolved === 'passed' ? '结果待核验' : '未通过', type: resolved === 'passed' ? 'warning' : 'danger' }
}
const reasons: Record<string, string> = {
  BUDGET_EXCEEDED: '本任务的调用额度或等待时间已用完，保留已有报告；这不表示质量通过。',
  REPAIR_NO_PROGRESS: '相同稿件和问题未取得进展，已停止重复修改。',
  REWRITE_LIMIT: '已达到正文返工上限，保留已有报告。',
  MAX_ITERATIONS: '已达到审核与返工上限，保留已有报告。',
  RESEARCH_LIMIT: '已达到补充研究上限，资料缺口仍需核验。',
  REVIEW_UNAVAILABLE: '审核响应不可用，无法据此确认整稿质量。',
  DISPUTE_UNRESOLVED: '实质争议仍待核验，暂不能判定通过。',
  NO_ACTIONABLE_TARGETS: '当前没有可靠的修改目标，已保留报告并停止修改。',
  REVIEW_NO_ACTIONABLE_FINDINGS: '审核未提供可靠的修改目标，已保留报告。',
  NO_CONVERGENCE: '复审未确认问题得到实质解决，已停止重复修改。',
  REWRITE_FAILED: '返工未获接受，已保留原稿。',
  REWRITE_NO_CHANGE: '没有可采用的正文变化，已保留原稿。',
  QUALITY_PASSED: '已完成整稿审核。',
}
export function stopExplanation(reason?: string | null): string {
  return reason ? reasons[reason] || '流程已停止；具体原因请展开技术明细查看。' : ''
}
