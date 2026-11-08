<script setup lang="ts">
import type { ReviewRound } from '../api/tasks'
defineProps<{ round: ReviewRound }>()
</script>

<template>
  <details>
    <summary>声明依据与争议复核</summary>
    <p v-if="!round.claim_bindings">此记录未记录逐项事实绑定。</p>
    <div v-for="(claim, index) in round.claim_bindings" :key="index">
      <p>报告声明：{{ claim.text }}</p>
      <p v-for="id in claim.fact_ids" :key="id">
        {{ round.fact_registry?.[id]?.source_id || '未记录来源' }} 原文：
        {{ round.fact_registry?.[id]?.quote || '未记录' }}
      </p>
    </div>
    <template v-if="round.dispute_review">
      <p>争议复核{{ round.dispute_review.status === 'reviewed' ? '已完成' : '待核实，保留拒绝' }}</p>
      <details>
        <summary>查看原意见与复核依据</summary>
        <pre>{{ JSON.stringify(round.dispute_review, null, 2) }}</pre>
      </details>
    </template>
  </details>
</template>

<style scoped>
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 24rem; overflow-y: auto; }
</style>
