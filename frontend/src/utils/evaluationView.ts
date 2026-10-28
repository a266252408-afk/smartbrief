import type { EvalMetrics, EvalReportSummary } from '../api/evaluation'

/** 评测中心展示口径的纯函数；避免把换算逻辑散落在模板里，便于单测。 */

export interface MetricCard {
  key: string
  label: string
  value: string
  detail?: string
  /** null 表示只展示、不参与达标判定（无目标或样本未完整）。 */
  target: string | null
  /** null 表示不显示“达标/未达标”标签。 */
  met: boolean | null
}

export interface RadarView {
  labels: string[]
  values: number[]
}

/** 比率格式化为百分比；null（未跑完/无数据）显示破折号。 */
export function formatPercent(value: number | null | undefined): string {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}

/** 目标值=100：低于目标按比例得分，达到或优于目标封顶 100。 */
export function targetScore(value: number | null | undefined, target: number, hasSamples: boolean): number {
  if (!hasSamples) return 0
  if (value == null || value <= 0) return 100
  return Math.max(0, Math.min(100, (target / value) * 100))
}

function metOf(met: Record<string, boolean | null>, key: string): boolean | null {
  const value = met[key]
  return typeof value === 'boolean' ? value : null
}

function percentTarget(value: number | undefined): string | null {
  return typeof value === 'number' ? `${(value * 100).toFixed(1)}%` : null
}

function seconds(value: number | null | undefined): string {
  return `${(value ?? 0).toFixed(1)}s`
}

function loops(value: number | null | undefined): string {
  return `${(value ?? 0).toFixed(1)} 次`
}

function secondsTarget(value: number | undefined): string | null {
  return typeof value === 'number' ? `${value.toFixed(1)}s` : null
}

function loopsTarget(value: number | undefined): string | null {
  return typeof value === 'number' ? `${value.toFixed(1)} 次` : null
}

function round1(value: number): number {
  return Math.round(value * 10) / 10
}

/**
 * 指标卡：v3 分开“正常用例质量”和“故障处理”，整体质量通过率只放在
 * 协议卡/Markdown 里作参考；执行完成率与 P95 无目标，只展示不判达标。
 */
export function buildMetricCards(metrics: EvalMetrics): MetricCard[] {
  const summary = metrics.summary
  const targets = metrics.targets ?? {}
  const met = metrics.met ?? {}
  const version = summary.metric_version ?? 0
  const cards: MetricCard[] = []

  if (version >= 2) {
    cards.push({
      key: 'execution_success_rate',
      label: '执行完成率',
      value: formatPercent(summary.execution_success_rate ?? 0),
      detail: `${summary.execution_success_cases ?? 0}/${summary.total_cases} 例执行完成`,
      target: null,
      met: null
    })
  }

  if (version >= 3) {
    cards.push({
      key: 'normal_quality_pass_rate',
      label: '正常用例质量通过率',
      value: formatPercent(summary.normal_quality_pass_rate),
      detail: summary.normal_total_cases == null
        ? '未跑完全部用例或旧版记录，暂不判定'
        : `${summary.normal_passed_cases ?? 0}/${summary.normal_total_cases} 例非故障用例`,
      target: percentTarget(targets.normal_quality_pass_rate),
      met: metOf(met, 'normal_quality_pass_rate')
    })
    cards.push({
      key: 'fault_handling_rate',
      label: '故障处理正确率',
      value: formatPercent(summary.fault_handling_rate),
      detail: summary.fault_total_cases == null
        ? '未跑完全部用例或旧版记录，暂不判定'
        : `${summary.fault_handled_cases ?? 0}/${summary.fault_total_cases} 例故障用例；误放行 ${summary.fault_false_pass_cases ?? 0} 例`,
      target: percentTarget(targets.fault_handling_rate),
      met: metOf(met, 'fault_handling_rate')
    })
  } else {
    cards.push({
      key: 'success_rate',
      label: '旧版执行成功率',
      value: formatPercent(summary.success_rate),
      target: percentTarget(targets.success_rate),
      met: metOf(met, 'success_rate')
    })
  }

  cards.push(
    {
      key: 'avg_duration_seconds',
      label: '平均执行耗时',
      value: seconds(summary.avg_duration_seconds),
      target: secondsTarget(targets.avg_duration_seconds),
      met: metOf(met, 'avg_duration_seconds')
    },
    {
      key: 'p95_duration_seconds',
      label: 'P95 耗时',
      value: seconds(summary.p95_duration_seconds),
      detail: '仅执行完成用例',
      target: null,
      met: null
    },
    {
      key: 'avg_react_loops',
      label: '平均 ReAct 循环',
      value: loops(summary.avg_react_loops),
      target: loopsTarget(targets.avg_react_loops),
      met: metOf(met, 'avg_react_loops')
    },
    {
      key: 'keyword_coverage_rate',
      label: '关键词覆盖率',
      value: formatPercent(summary.keyword_coverage_rate),
      target: percentTarget(targets.keyword_coverage_rate),
      met: metOf(met, 'keyword_coverage_rate')
    },
    {
      key: 'memory_hit_rate',
      label: '记忆命中率',
      value: formatPercent(summary.memory_hit_rate),
      target: percentTarget(targets.memory_hit_rate),
      met: metOf(met, 'memory_hit_rate')
    }
  )
  return cards
}

/** 雷达图五个维度；耗时/循环以目标值为 100 分，达到目标即满分。 */
export function buildRadarView(metrics: EvalMetrics): RadarView {
  const summary = metrics.summary
  const targets = metrics.targets ?? {}
  const version = summary.metric_version ?? 0
  // 历史 v3 记录没有分组字段时退回整体通过率；本次运行未跑完则按未知（0）展示。
  const quality = version >= 3
    ? (summary.normal_quality_pass_rate
      ?? (summary.normal_total_cases == null ? summary.success_rate ?? 0 : 0))
    : (summary.success_rate ?? 0)
  const durationTarget = targets.avg_duration_seconds ?? 120
  const loopTarget = targets.avg_react_loops ?? 8
  const durationSamples = (summary.execution_success_cases ?? summary.total_cases ?? 0) > 0
  const loopSamples = (summary.total_cases ?? 0) > 0
  return {
    labels: [
      version >= 3 ? '正常用例质量通过率' : '旧版执行成功率',
      '关键词覆盖率',
      '记忆命中率',
      '耗时达标分',
      '循环达标分'
    ],
    values: [
      round1(quality * 100),
      round1((summary.keyword_coverage_rate ?? 0) * 100),
      round1((summary.memory_hit_rate ?? 0) * 100),
      round1(targetScore(summary.avg_duration_seconds, durationTarget, durationSamples)),
      round1(targetScore(summary.avg_react_loops, loopTarget, loopSamples))
    ]
  }
}

/** 历史列表：执行完成率（旧记录没有指标时返回 null）。 */
export function executionRate(item: EvalReportSummary): number | null {
  return item.metrics?.summary?.execution_success_rate ?? null
}

/** 历史列表：v3 显示正常用例质量通过率；旧 v3 无分组字段时退回整体通过率。 */
export function qualityRate(item: EvalReportSummary): number | null {
  const summary = item.metrics?.summary
  if (!summary) return null
  if ((summary.metric_version ?? 0) >= 3) {
    if (summary.normal_quality_pass_rate != null) return summary.normal_quality_pass_rate
    // normal_total_cases 存在说明是本次运行但尚未跑完，不能拿部分样本冒充成绩。
    return summary.normal_total_cases == null ? (summary.success_rate ?? null) : null
  }
  return summary.success_rate ?? null
}
