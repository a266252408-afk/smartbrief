import { describe, expect, it } from 'vitest'
import {
  buildMetricCards,
  buildRadarView,
  executionRate,
  formatPercent,
  qualityRate,
  targetScore
} from '../evaluationView'
import type { EvalMetrics, EvalReportSummary } from '../../api/evaluation'

function metrics(): EvalMetrics {
  return {
    summary: {
      metric_version: 3,
      execution_success_rate: 0.85,
      execution_success_cases: 17,
      total_cases: 20,
      success_cases: 15,
      failed_cases: 5,
      success_rate: 0.75,
      normal_total_cases: 16,
      normal_passed_cases: 15,
      normal_quality_pass_rate: 0.9375,
      fault_total_cases: 4,
      fault_handled_cases: 4,
      fault_false_pass_cases: 0,
      fault_handling_rate: 1,
      avg_duration_seconds: 0.5,
      p95_duration_seconds: 0.58,
      avg_react_loops: 2.9,
      keyword_coverage_rate: 0.85,
      memory_hit_rate: 0.85
    },
    targets: {
      success_rate: 0.9,
      normal_quality_pass_rate: 0.9,
      fault_handling_rate: 1,
      avg_duration_seconds: 120,
      avg_react_loops: 8,
      keyword_coverage_rate: 0.7,
      memory_hit_rate: 0.3
    },
    met: {
      success_rate: false,
      normal_quality_pass_rate: true,
      fault_handling_rate: true,
      avg_duration_seconds: true,
      avg_react_loops: true,
      keyword_coverage_rate: true,
      memory_hit_rate: true
    }
  }
}

function summaryItem(data: EvalMetrics): EvalReportSummary {
  return {
    eval_id: 'eval_x',
    status: 'completed',
    total_cases: data.summary.total_cases,
    completed_cases: 20,
    error: null,
    created_at: '2026-10-27',
    updated_at: '2026-10-27',
    metrics: data
  }
}

describe('评测中心指标展示', () => {
  it('v3 分开显示正常用例质量与故障处理，不把整体通过率当目标', () => {
    const cards = buildMetricCards(metrics())
    expect(cards.map((card) => card.label)).toEqual([
      '执行完成率',
      '正常用例质量通过率',
      '故障处理正确率',
      '平均执行耗时',
      'P95 耗时',
      '平均 ReAct 循环',
      '关键词覆盖率',
      '记忆命中率'
    ])
    const byKey = Object.fromEntries(cards.map((card) => [card.key, card]))
    expect(byKey.execution_success_rate).toMatchObject({ target: null, met: null })
    expect(byKey.normal_quality_pass_rate).toMatchObject({ value: '93.8%', target: '90.0%', met: true })
    expect(byKey.fault_handling_rate).toMatchObject({ value: '100.0%', target: '100.0%', met: true })
    expect(byKey.p95_duration_seconds).toMatchObject({ target: null, met: null })
    expect(byKey.success_rate).toBeUndefined()
  })

  it('雷达图以目标值为 100：刚好达标不再算 0 分', () => {
    const view = buildRadarView(metrics())
    expect(view.labels[0]).toBe('正常用例质量通过率')
    expect(view.values[0]).toBe(93.8)
    expect(view.values[3]).toBe(100) // 0.5s 远优于 120s 目标
    expect(view.values[4]).toBe(100) // 2.9 次优于 8 次目标

    const slow = metrics()
    slow.summary.avg_duration_seconds = 240
    slow.summary.avg_react_loops = 16
    expect(buildRadarView(slow).values.slice(3)).toEqual([50, 50])
  })

  it('目标分换算：达到目标满分，超出目标按比例扣分', () => {
    expect(targetScore(8, 8, true)).toBe(100)
    expect(targetScore(16, 8, true)).toBe(50)
    expect(targetScore(0, 8, true)).toBe(100)
    expect(targetScore(8, 8, false)).toBe(0)
  })

  it('v2 旧记录退回旧版成功率口径，不伪造分组指标', () => {
    const legacy = metrics()
    legacy.summary.metric_version = 2
    delete legacy.summary.normal_total_cases
    delete legacy.summary.normal_passed_cases
    delete legacy.summary.normal_quality_pass_rate
    delete legacy.summary.fault_total_cases
    delete legacy.summary.fault_handled_cases
    delete legacy.summary.fault_false_pass_cases
    delete legacy.summary.fault_handling_rate

    const cards = buildMetricCards(legacy)
    expect(cards.some((card) => card.key === 'success_rate')).toBe(true)
    expect(cards.some((card) => card.key === 'normal_quality_pass_rate')).toBe(false)
    expect(buildRadarView(legacy).labels[0]).toBe('旧版执行成功率')
  })

  it('列表使用正常用例质量；未跑完或旧记录显示未知', () => {
    const item = summaryItem(metrics())
    expect(executionRate(item)).toBe(0.85)
    expect(qualityRate(item)).toBe(0.9375)

    const running = summaryItem(metrics())
    running.metrics!.summary.normal_quality_pass_rate = null
    expect(qualityRate(running)).toBeNull()

    // 历史 v3 记录没有分组字段，退回整体通过率，不显示为空。
    const historical = summaryItem(metrics())
    historical.metrics!.summary.normal_quality_pass_rate = null
    delete historical.metrics!.summary.normal_total_cases
    expect(qualityRate(historical)).toBe(0.75)
    expect(buildRadarView(historical.metrics!).values[0]).toBe(75)

    expect(qualityRate({ ...item, metrics: null })).toBeNull()
    expect(formatPercent(null)).toBe('—')
  })
})
