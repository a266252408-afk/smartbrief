<script setup lang="ts">
import type { RewriteAudit } from '../api/tasks'
defineProps<{ audit: RewriteAudit }>()
const outcomes: Record<string, string> = {
  candidate_applied_after_restriction: '程序曾拦截修改，升级或重试后已应用候选稿',
  partial_candidate_applied: '程序拦截了部分修改；保留局部候选，全文尝试未成功',
  candidate_blocked: '程序未接受候选修改，原稿保留',
  candidate_applied: '候选修改已应用，未记录程序拦截',
  citation_obligations_updated: '已更新声明删除回执，正文未变，等待独立复审',
  no_change_proposed: 'Agent 未提出可落稿的正文变化',
  model_call_failed: '模型调用失败，未取得可应用的候选稿',
  context_blocked: '必需证据超出程序上下文上限，未调用返工模型',
}
const reported: Record<string, string> = { fixed: '已修复', skipped: '跳过', cannot_fix: '无法修复' }
const verified: Record<string, string> = { resolved: '问题已消失', unresolved: '仍未解决', unverified: '待独立复审' }
</script>

<template>
  <details data-testid="rewrite-audit">
    <summary>返工{{ audit.changed || audit.citation_changes_applied ? '已应用' : '未应用' }} · {{ audit.mode }}</summary>
    <template v-if="audit.program_restriction">
      <p>{{ outcomes[audit.program_restriction.outcome] || '返工诊断状态未知' }}</p>
      <p>程序是否拦截过提案：{{ audit.program_restriction.observed ? '是' : '否' }}。被拦截的修改是否能解决质量问题，仍需核对提案和复审结果。</p>
    </template>
    <p v-else>此记录缺少完整诊断，无法判断程序是否限制了修改。</p>
    <div v-if="audit.verification?.length">
      <p>逐项修改核对；整体质量仍以独立复审为准。</p>
      <p v-for="item in audit.verification" :key="item.finding_id">
        {{ item.finding_id }}：模型自报{{ reported[item.reported_status || ''] || '未提供' }}；实际检查{{ verified[item.status] }}。{{ item.reason }}
      </p>
    </div>
    <p v-for="item in audit.citation_lifecycle?.retired_claims" :key="item.source_id">
      {{ item.source_id }}：{{ item.reason }}{{ item.review_required ? '；已提供原声明供独立审核复核。' : '' }}
    </p>
    <p v-for="(failure, index) in audit.failures" :key="index">{{ failure }}</p>
    <details v-for="(attempt, index) in audit.attempts" :key="index">
      <summary>第 {{ index + 1 }} 次尝试 · {{ attempt.mode }} · {{ attempt.decision }}</summary>
      <p v-if="attempt.request">允许范围：{{ attempt.request.scope === 'full' ? '全文重组' : `段落 ${attempt.request.allowed_paragraphs.join('、')}` }}；{{ attempt.request.reason }}</p>
      <p v-if="attempt.request?.missing_evidence_ids?.length">未进入返工上下文的必需来源：{{ attempt.request.missing_evidence_ids.join('、') }}</p>
      <p v-if="attempt.message">{{ attempt.code }}：{{ attempt.message }}</p>
      <p v-for="item in attempt.verification" :key="item.finding_id">
        {{ item.finding_id }}：自报{{ reported[item.reported_status || ''] || '未提供' }}，检查{{ verified[item.status] }}。
      </p>
      <p>接受 {{ attempt.accepted?.length || 0 }} 条补丁，拦截 {{ attempt.rejected?.length || 0 }} 条补丁</p>
      <div v-for="rejected in attempt.rejected" :key="rejected.index">
        <p>第 {{ rejected.index + 1 }} 条 · {{ rejected.code }}：{{ rejected.message }}</p>
        <pre>{{ JSON.stringify(rejected.proposal, null, 2) }}</pre>
      </div>
      <details v-if="attempt.raw_response !== undefined">
        <summary>查看 Agent 原始提案{{ attempt.raw_truncated ? '（仅保存前 64000 字符，原文已截断）' : '' }}</summary>
        <pre>{{ attempt.raw_response }}</pre>
      </details>
    </details>
  </details>
</template>

<style scoped>
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 24rem; overflow-y: auto; }
details { margin: 8px 0; }
</style>
