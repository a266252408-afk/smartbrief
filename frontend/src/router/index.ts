import { createRouter, createWebHistory } from 'vue-router'

import Chat from '../views/Chat.vue'
import ToolMonitor from '../views/ToolMonitor.vue'
import SkillCenter from '../views/SkillCenter.vue'
import Home from '../views/Home.vue'
import Tasks from '../views/Tasks.vue'
import TaskDetail from '../views/TaskDetail.vue'
import KnowledgeBase from '../views/KnowledgeBase.vue'
import Evaluation from '../views/Evaluation.vue'
import Reports from '../views/Reports.vue'
import ReportView from '../views/ReportView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      redirect: '/home'
    },
    {
      path: '/home',
      name: 'home',
      component: Home,
      meta: { title: '任务提交' }
    },
    {
      path: '/tasks',
      name: 'tasks',
      component: Tasks,
      meta: { title: '任务列表' }
    },
    {
      path: '/tasks/:taskId',
      name: 'task-detail',
      component: TaskDetail,
      meta: { title: '任务详情' }
    },
    {
      path: '/knowledge',
      name: 'knowledge',
      component: KnowledgeBase,
      meta: { title: '知识库' }
    },
    {
      path: '/evaluation',
      name: 'evaluation',
      component: Evaluation,
      meta: { title: '评测中心' }
    },
    {
      path: '/reports',
      name: 'reports',
      component: Reports,
      meta: { title: '报告中心' }
    },
    {
      path: '/report/:taskId',
      name: 'report',
      component: ReportView,
      meta: { title: '报告展示' }
    },
    {
      path: '/chat',
      name: 'chat',
      component: Chat,
      meta: { title: 'AI 对话' }
    },
    {
      path: '/tools',
      name: 'tools',
      component: ToolMonitor,
      meta: { title: '工具监控' }
    },
    {
      path: '/skills',
      name: 'skills',
      component: SkillCenter,
      meta: { title: '技能中心' }
    }
  ]
})

router.afterEach((to) => {
  document.title = `${to.meta.title as string} · SmartBrief`
})

export default router
