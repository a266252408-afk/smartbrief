<script setup lang="ts">
import type { TaskMetadata } from '../api/tasks'

defineProps<{ metadata?: TaskMetadata }>()
</script>

<template>
  <el-tag
    v-if="metadata?.demo"
    type="warning"
    effect="dark"
    size="small"
    class="demo-badge"
    data-testid="demo-badge"
    title="显式导入的演示样例，不调用模型，不代表在线生成成绩"
  >
    演示数据
  </el-tag>
</template>

<style scoped>
.demo-badge {
  margin-left: 8px;
}
</style>
