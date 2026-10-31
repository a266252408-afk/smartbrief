import { describe, expect, it } from 'vitest'
import routerSource from '../index.ts?raw'
import { routes } from '../index'

const pageRoutes = routes.filter((route) => !route.redirect)

describe('路由按需加载', () => {
  it('所有页面路由使用加载函数，静态导入会使 component 变成组件对象', () => {
    expect(pageRoutes).toHaveLength(16)
    for (const route of pageRoutes) {
      expect(typeof route.component, String(route.path)).toBe('function')
    }
  })

  it('加载函数返回页面模块 Promise', async () => {
    const home = pageRoutes.find((route) => route.path === '/home')
    const loader = home?.component as (() => Promise<{ default: unknown }>) | undefined
    expect(loader).toBeTypeOf('function')
    const loaded = await loader!()
    expect(loaded.default).toBeTruthy()
  }, 20000)

  it('路由源码不静态导入页面，逐页使用 import()', () => {
    expect(routerSource).not.toMatch(/^import\s+\w+\s+from\s+'\.\.\/views\//m)
    const targets = [...routerSource.matchAll(/import\('\.\.\/views\/([A-Za-z]+)\.vue'\)/g)]
      .map((match) => match[1])
      .sort()
    expect(targets).toEqual([
      'Chat', 'Evaluation', 'EvaluationCenter', 'EvaluationCenter', 'Home', 'KnowledgeBase', 'ReportView', 'Reports',
      'SkillCenter', 'TaskDetail', 'Tasks', 'ToolMonitor'
    ])
  })

  it('保留页面路径与标题', () => {
    expect(pageRoutes.map((route) => [route.path, route.meta?.title])).toEqual([
      ['/home', '任务提交'],
      ['/tasks', '任务列表'],
      ['/tasks/:taskId', '任务详情'],
      ['/knowledge', '知识库'],
      ['/evaluation', '评测中心'],
      ['/evaluation/datasets', '评测中心'],
      ['/evaluation/run', '评测中心'],
      ['/evaluation/records', '评测中心'],
      ['/evaluation/review', '评测中心'],
      ['/evaluation/compare', '评测中心'],
      ['/evaluation/protocol', '原协议评测'],
      ['/reports', '报告中心'],
      ['/report/:taskId', '报告展示'],
      ['/chat', 'AI 对话'],
      ['/tools', '工具监控'],
      ['/skills', '技能中心']
    ])
  })
})
