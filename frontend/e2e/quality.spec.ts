import { test, expect } from '@playwright/test'

// 明确离线HTTP协议夹具；真实图/SQLite集成由后端测试独立验证。
for (const [quality, label, forced] of [
  ['passed', '审核通过', false], ['rejected', '未通过（强制输出）', true],
  ['unavailable', '审核不可用', false], ['unknown', '质量未知', false],
  ['not_run', '未审核', false], ['pending', '待审核', false]
] as const) {
  test(`质量状态 ${quality} 在详情、列表和报告一致`, async ({ page }) => {
    const unexpected: string[] = []
    const task = {
      task_id: 'quality-fixture', topic: '质量测试报告', status: 'completed', execution_status: 'completed',
      quality_status: quality, forced_output: forced, error_code: null,
      plan: [], completed_steps: [], current_step_index: 0, research_data: {}, react_logs: [],
      draft_content: '## 测试正文', final_report: '## 测试正文', error: null,
      review_comments: [], review_history: [], iteration: 0, max_iterations: 3,
      passed: true, forced_pass: forced, memory_hits: [], created_at: '', updated_at: ''
    }
    await page.route('**/*', async route => {
      if (new URL(route.request().url()).origin === 'http://127.0.0.1:5175') await route.continue()
      else { unexpected.push(route.request().url()); await route.abort() }
    })
    await page.route('**/api/v*/**', async route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/v2/auth/session') await route.fulfill({ json: { data: { role: 'admin' }, meta: { request_id: 'fixture' } } })
      else if (path === '/api/v1/tasks/quality-fixture/status') await route.fulfill({ json: task })
      else if (path === '/api/v1/tasks') await route.fulfill({ json: { items: [task], total: 1, page: 1, page_size: 10 } })
      else if (path === '/api/v1/tasks/quality-fixture/report') await route.fulfill({ json: {
        ...task, title: task.topic, content_markdown: task.final_report, charts: [],
        executive_summary: { total_time: 1, react_loops: 0, memory_hit_count: 0 }
      } })
      else { unexpected.push(path); await route.fulfill({ status: 500, json: { detail: '未声明请求' } }) }
    })
    for (const path of ['/tasks/quality-fixture', '/reports', '/report/quality-fixture']) {
      await page.goto(path)
      await page.getByLabel('访问令牌').fill('fixture-browser-quality')
      await page.getByRole('button', { name: '验证并进入' }).click()
      const badge = page.getByTestId('quality-status').first()
      await expect(badge).toHaveText(label)
      if (quality !== 'passed') {
        await expect(badge).not.toHaveClass(/el-tag--success/)
        await expect(page.getByText('审核通过', { exact: true })).toHaveCount(0)
        await expect(page.getByText('强制通过', { exact: true })).toHaveCount(0)
      }
    }
    expect(unexpected).toEqual([])
  })
}
