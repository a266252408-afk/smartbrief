<script setup lang="ts">
import { computed } from 'vue'
import type { ReviewRound } from '../api/tasks'
import { stopExplanation } from '../utils/reviewDiagnostics'
const props = defineProps<{ round: ReviewRound }>()
const counts = computed(() => {
  const result = { confirmed: 0, pending: 0, suggestions: 0, unknown: 0 }
  for (const finding of props.round.findings || []) {
    if (finding.decision === 'dismissed') continue
    const material = finding.material === true || finding.severity === 'hard'
    if (finding.decision === 'confirmed' && material) result.confirmed++
    else if ((finding.decision === 'suspected' || finding.decision === 'unresolved') && material) result.pending++
    else if (finding.decision) result.suggestions++
    else result.unknown++
  }
  return result
})
</script>

<template>
  <section class="review-diagnostics" aria-label="审核诊断">
    <div class="review-summary" data-testid="review-summary">
      <div class="issue-counts">
        <span>已确认问题 {{ counts.confirmed }} 项</span>
        <span>待核验疑点 {{ counts.pending }} 项</span>
        <span v-if="counts.suggestions">普通建议 {{ counts.suggestions }} 项</span>
        <span v-if="counts.unknown">历史问题 {{ counts.unknown }} 项（判定未记录）</span>
      </div>
      <p v-if="round.review_diagnostics">审核响应不可用或格式不可靠；这是审核自身的故障，不能仅据此认定正文有错。</p>
      <p v-if="round.stop_reason">{{ stopExplanation(round.stop_reason) }}</p>
      <p v-if="round.dispute_review">争议{{ round.dispute_review.status === 'reviewed' ? '核验已结束' : '待核验，质量尚未确认' }}；整稿质量仍以审核结果为准。</p>
      <p v-if="round.content_checks">核心问题记录：{{ round.content_checks.core_status === 'covered' ? '已记录逐项覆盖依据' : round.content_checks.core_status === 'missing' ? '存在覆盖缺口' : '未记录逐项覆盖，不能推断已覆盖' }}。</p>
    </div>
    <details>
      <summary>声明依据与争议复核</summary>
      <p v-if="!round.claim_bindings">此记录未记录逐项事实绑定。</p>
      <div v-for="(claim, index) in round.claim_bindings" :key="index">
        <p>报告声明：{{ claim.text }}</p>
        <p v-for="id in claim.fact_ids" :key="id">{{ round.fact_registry?.[id]?.source_id || '未记录来源' }} 原文：{{ round.fact_registry?.[id]?.quote || '未记录' }}</p>
      </div>
      <template v-if="round.dispute_review">
        <p>争议{{ round.dispute_review.status === 'reviewed' ? '核验已结束' : '待核验，质量尚未确认' }}；整稿质量仍以审核结果为准。</p>
        <details><summary>查看原意见与复核依据</summary><pre>{{ JSON.stringify(round.dispute_review, null, 2) }}</pre></details>
      </template>
      <div v-for="row in round.content_checks?.core_coverage" :key="row.question">
        <p>{{ row.question }}：{{ row.covered ? '已记录覆盖依据' : '覆盖不足' }}。{{ row.reason }}</p><blockquote>{{ row.quote }}</blockquote>
      </div>
    </details>
    <details data-testid="review-technical">
      <summary>查看审核技术明细</summary>
      <pre>{{ JSON.stringify({ stop_reason: round.stop_reason, review_diagnostics: round.review_diagnostics, repair_plan: round.repair_plan, action_plan: round.action_plan, findings: round.findings, content_checks: round.content_checks }, null, 2) }}</pre>
    </details>
  </section>
</template>

<style scoped>
.review-diagnostics { margin: 12px 0; }
.review-summary { border: 1px solid #dce5ee; border-radius: 8px; background: #f7fafc; padding: 12px 14px; color: #334155; }
.issue-counts { display: flex; gap: 8px 20px; flex-wrap: wrap; font-weight: 600; }
p { line-height: 1.7; margin: 8px 0; }
details { margin-top: 8px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 24rem; overflow-y: auto; }
blockquote { margin: 6px 0; padding-left: 12px; border-left: 3px solid #cbd5e1; }
</style>
