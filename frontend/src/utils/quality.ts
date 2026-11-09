import type { QualityStatus } from '../api/tasks'

/** 只使用明确质量状态；缺失字段的历史记录不推断通过。 */
export function qualityInfo(quality?: QualityStatus, forced = false) {
  if (forced && quality === 'rejected') return { label: '未通过（强制输出）', type: 'warning' as const }
  if (quality === 'passed') return { label: '审核通过', type: 'success' as const }
  if (quality === 'rejected') return { label: '未通过', type: 'danger' as const }
  if (quality === 'unavailable') return { label: '审核不可用', type: 'warning' as const }
  if (quality === 'not_run') return { label: '未审核', type: 'info' as const }
  if (quality === 'pending') return { label: '待审核', type: 'primary' as const }
  return { label: '质量未知', type: 'info' as const }
}
