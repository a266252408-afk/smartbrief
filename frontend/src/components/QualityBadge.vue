<script setup lang="ts">
import { computed } from 'vue'
import type { QualityStatus } from '../api/tasks'
import { qualityInfo } from '../utils/quality'
const props = defineProps<{ quality?: QualityStatus; forced?: boolean }>()
const info = computed(() => qualityInfo(props.quality, props.forced))
</script>

<template>
  <el-tag data-testid="quality-status" :type="info.type" size="small">{{ info.label }}</el-tag>
</template>
