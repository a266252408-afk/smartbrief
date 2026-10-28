import { test, expect } from '@playwright/test'

for (const legacy of [false, true]) {
  test(`离线任务不能从列表或详情恢复与重生成（旧记录=${legacy}）`, async ({ page }) => {
    const mutations: string[] = []
    const task = { task_id: 'offline-fixture', topic: legacy ? '[离线协议] 旧案例' : '离线案例',
      metadata: legacy ? {} : { execution_mode: 'offline', evaluation_id: 'eval_fixture', evaluation_case_id: 'C01' },
      status: 'failed', execution_status: 'failed', quality_status: 'not_run', forced_output: false,
      error_code: 'BUDGET_EXCEEDED', plan: [], completed_steps: [], current_step_index: 0,
      research_data: {}, react_logs: [], review_comments: [], review_history: [], memory_hits: [],
      draft_content: '', final_report: null, iteration: 0, max_iterations: 3, created_at: '', updated_at: '' }
    await page.route('**/api/v*/**', async route => {
      const request = route.request(), path = new URL(request.url()).pathname
      if (path.endsWith('/auth/session')) await route.fulfill({ json: { data: { role: 'admin' }, meta: {} } })
      else if (request.method() !== 'GET') { mutations.push(path); await route.abort() }
      else if (path.endsWith('/status')) await route.fulfill({ json: task })
      else if (path === '/api/v1/tasks') await route.fulfill({ json: { items: [task], total: 1, page: 1, page_size: 10 } })
      else await route.fulfill({ status: 404, json: {} })
    })
    await page.goto('/tasks')
    await page.getByLabel('访问令牌').fill('fixture-isolation')
    await page.getByRole('button', { name: '验证并进入' }).click()
    await expect(page.getByText('离线协议任务，请在评测中心重新运行')).toBeVisible()
    await expect(page.getByRole('button', { name: '恢复任务', exact: true })).toHaveCount(0)
    await page.getByRole('button', { name: '查看', exact: true }).click()
    await expect(page.getByText('离线协议任务，请在评测中心重新运行')).toBeVisible()
    await expect(page.getByRole('button', { name: '恢复任务', exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: '重新生成', exact: true })).toHaveCount(0)
    expect(mutations).toEqual([])
  })
}
