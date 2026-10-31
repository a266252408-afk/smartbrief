import { ref } from 'vue'
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

// 懒加载页面块失败时给出可恢复提示；只保存用户可见路径，不输出错误堆栈或查询凭证。
export const routeLoadError = ref('')
export function reportRouteLoadError(path: string) {
  routeLoadError.value = `页面加载失败：${path}，请刷新重试`
}

// 页面全部使用动态导入：首屏只加载入口与当前路由，避免评测/导出/图表等依赖进入公共包。
export const routes: RouteRecordRaw[] = [
  {
    path: '/',
    redirect: '/home'
  },
  {
    path: '/home',
    name: 'home',
    component: () => import('../views/Home.vue'),
    meta: { title: '任务提交' }
  },
  {
    path: '/tasks',
    name: 'tasks',
    component: () => import('../views/Tasks.vue'),
    meta: { title: '任务列表' }
  },
  {
    path: '/tasks/:taskId',
    name: 'task-detail',
    component: () => import('../views/TaskDetail.vue'),
    meta: { title: '任务详情' }
  },
  {
    path: '/knowledge',
    name: 'knowledge',
    component: () => import('../views/KnowledgeBase.vue'),
    meta: { title: '知识库' }
  },
  {
    path: '/evaluation',
    name: 'evaluation',
    component: () => import('../views/EvaluationCenter.vue'),
    meta: { title: '评测中心', evaluationTab: 'overview' }
  },
  ...(['datasets', 'run', 'records', 'review', 'compare'] as const).map(tab => ({
    path: `/evaluation/${tab}`, name: `evaluation-${tab}`,
    component: () => import('../views/EvaluationCenter.vue'),
    meta: { title: '评测中心', evaluationTab: tab }
  })),
  { path: '/evaluation/protocol', name: 'evaluation-protocol', component: () => import('../views/Evaluation.vue'), meta: { title: '原协议评测' } },
  {
    path: '/reports',
    name: 'reports',
    component: () => import('../views/Reports.vue'),
    meta: { title: '报告中心' }
  },
  {
    path: '/report/:taskId',
    name: 'report',
    component: () => import('../views/ReportView.vue'),
    meta: { title: '报告展示' }
  },
  {
    path: '/chat',
    name: 'chat',
    component: () => import('../views/Chat.vue'),
    meta: { title: 'AI 对话' }
  },
  {
    path: '/tools',
    name: 'tools',
    component: () => import('../views/ToolMonitor.vue'),
    meta: { title: '工具监控' }
  },
  {
    path: '/skills',
    name: 'skills',
    component: () => import('../views/SkillCenter.vue'),
    meta: { title: '技能中心' }
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

router.afterEach((to) => {
  routeLoadError.value = ''
  document.title = `${to.meta.title as string} · SmartBrief`
})

router.onError((_error, to) => {
  reportRouteLoadError(to.path)
})

export default router
