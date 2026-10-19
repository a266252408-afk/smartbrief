import { test, expect } from '@playwright/test'

// 仅为界面协议夹具，真实后端工作流另由后端集成测试验收。
const fixture = {
  task_id: 'fixture-browser', topic: '离线浏览器主题', status: 'completed',
  plan: [{ id: 1, agent: 'writer', task: '撰写测试报告', depends_on: [] }],
  current_step_index: 1, completed_steps: [1], created_at: '2026-10-19 12:00:00',
  updated_at: '2026-10-19 12:00:01', research_data: {}, draft_content: '## 离线夹具报告',
  final_report: '## 离线夹具报告', error: null, react_logs: [], review_comments: [],
  review_history: [], iteration: 0, max_iterations: 3, passed: true, forced_pass: false,
  memory_hits: []
}

for (const failFirst of [false, true]) {
  test(failFirst ? '服务失败后提示并可重试进入详情' : '提交主题并展示任务详情', async ({ page }) => {
    const requests: unknown[] = []
    const unexpected: string[] = []
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.origin === 'http://127.0.0.1:5175') await route.continue()
      else { unexpected.push(url.origin); await route.abort() }
    })
    await page.route('**/api/v1/**', async route => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      if (path === '/api/v1/generate' && request.method() === 'POST') {
        requests.push(request.postDataJSON())
        if (failFirst && requests.length === 1) {
          await route.fulfill({ status: 503, json: { detail: '固定服务故障' } })
        } else {
          await route.fulfill({ json: { task_id: fixture.task_id, status: 'running', topic: fixture.topic } })
        }
      } else if (path === `/api/v1/tasks/${fixture.task_id}/status`) {
        await route.fulfill({ json: fixture })
      } else {
        unexpected.push(path)
        await route.fulfill({ status: 500, json: { detail: '未声明的测试请求' } })
      }
    })
    await page.goto('/home')
    await expect(page.getByRole('heading', { name: '多智能体报告生成' })).toBeVisible()
    await page.getByRole('button', { name: '生成研究报告' }).click()
    await expect(page.getByText('请输入研究主题', { exact: true })).toBeVisible()
    expect(requests).toHaveLength(0)
    await page.getByRole('textbox').fill('  离线浏览器主题  ')
    await page.getByRole('button', { name: '生成研究报告' }).click()
    if (failFirst) {
      await expect(page.getByText('固定服务故障', { exact: true })).toBeVisible()
      await expect(page).toHaveURL(/\/home$/)
      await page.getByRole('button', { name: '生成研究报告' }).click()
    }
    await expect(page).toHaveURL(/\/tasks\/fixture-browser$/)
    await expect(page.getByText('离线浏览器主题', { exact: true })).toBeVisible()
    await expect(page.getByText('离线夹具报告', { exact: true })).toBeVisible()
    expect(requests).toEqual(Array(failFirst ? 2 : 1).fill({ topic: fixture.topic }))
    expect(unexpected).toEqual([])
  })
}
