<script setup lang="ts">
import { useRoute } from 'vue-router'
import AuthPanel from './components/AuthPanel.vue'
import { authRole, authNotice, clearSession } from './api/session'
import { routeLoadError } from './router'

const route = useRoute()

function reloadPage() {
  window.location.reload()
}

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
      <nav v-if="authRole" class="app-nav">
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
      <span v-if="authRole">{{ authRole === 'admin' ? '管理员' : '只读访问' }}</span>
      <el-button v-if="authRole" @click="clearSession()">退出认证</el-button>
    </header>
    <main class="app-main">
      <p v-if="routeLoadError" role="alert" class="route-error">
        {{ routeLoadError }}
        <el-button size="small" @click="reloadPage">刷新页面</el-button>
      </p>
      <template v-if="authRole">
        <p v-if="authNotice" role="alert">{{ authNotice }}</p>
        <router-view />
      </template>
      <AuthPanel v-else />
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

.route-error {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  color: #b42318;
  background: #fef3f2;
  border: 1px solid #fecdca;
  border-radius: 4px;
}
/* 窄屏导航换行，避免把页面撑宽或把中文菜单挤成竖排。 */
@media(max-width:1100px){
  .app-header{flex-wrap:wrap;padding:16px 24px}
  .app-nav{order:1;width:100%;margin-left:0;flex-wrap:wrap;gap:8px 16px}
  .nav-item{white-space:nowrap}
}
@media(max-width:600px){
  .app-header{padding:16px}
  .app-subtitle{flex-basis:100%}
  .app-main{padding:0 16px}
}
</style>
