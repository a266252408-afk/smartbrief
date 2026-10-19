<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()

const menus = [
  { path: '/home', label: '任务提交' },
  { path: '/tasks', label: '任务列表' },
  { path: '/reports', label: '报告中心' },
  { path: '/knowledge', label: '知识库' },
  { path: '/evaluation', label: '评测中心' },
  { path: '/chat', label: 'AI 对话' },
  { path: '/tools', label: '工具监控' },
  { path: '/skills', label: '技能中心' }
]
</script>

<template>
  <div class="app">
    <header class="app-header">
      <h1>SmartBrief</h1>
      <span class="app-subtitle">多智能体报告生成平台</span>
      <nav class="app-nav">
        <router-link
          v-for="m in menus"
          :key="m.path"
          :to="m.path"
          class="nav-item"
          :class="{ active: route.path === m.path }"
        >
          {{ m.label }}
        </router-link>
      </nav>
    </header>
    <main class="app-main">
      <router-view />
    </main>
  </div>
</template>

<style>
body {
  margin: 0;
  background: #f5f7fa;
}

.app-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 32px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}

.app-header h1 {
  margin: 0;
  font-size: 22px;
  color: #303133;
}

.app-subtitle {
  color: #909399;
  font-size: 14px;
}

.app-nav {
  margin-left: 32px;
  display: flex;
  gap: 20px;
}

.nav-item {
  color: #606266;
  text-decoration: none;
  font-size: 15px;
  padding: 4px 2px;
  border-bottom: 2px solid transparent;
}

.nav-item:hover {
  color: #409eff;
}

.nav-item.active {
  color: #409eff;
  border-bottom-color: #409eff;
}

.app-main {
  max-width: 1200px;
  margin: 24px auto;
  padding: 0 24px;
}
</style>
