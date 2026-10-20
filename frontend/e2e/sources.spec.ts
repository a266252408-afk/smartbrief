import { test, expect } from '@playwright/test'
import { readFile } from 'node:fs/promises'

// 界面协议夹具；真实后端来源与返工持久化另有离线集成测试。
for (const unsafe of [false, true]) {
  test(unsafe ? '未知与恶意来源不可点击或执行' : '详情与报告引用可回溯，Word和PDF保留来源', async ({ page }) => {
    test.setTimeout(60000)
    const unexpected: string[] = []
    const sources = [{ source_id: 'S1', task_id: 'source-fixture', kind: 'web', title: '明确来源',
      excerpt: '原始证据片段：本条为离线测试资料。', url: 'https://example.invalid/evidence',
      retrieved_at: '2026-10-20T00:00:00Z', source_deleted: false }, ...(unsafe ? [{
        source_id: 'S2', task_id: 'source-fixture', kind: 'web', title: '<img src=x onerror="window.__sourceInjected=1">',
        excerpt: '<script>window.__sourceInjected=1</script>', url: 'javascript:alert(1)',
        retrieved_at: '2026-10-20T00:00:00Z', source_deleted: false
      }] : [])]
    const markdown = '## 测试正文\n\n声明引用[S1]。' + (unsafe ? '\n\n未知[S999]，恶意来源[S2]。\n\n<img src=x onerror="window.__sourceInjected=1">' : '')
    const task = { task_id: 'source-fixture', topic: '来源测试报告', status: 'completed', execution_status: 'completed',
      quality_status: unsafe ? 'rejected' : 'passed', forced_output: false, error_code: null,
      evidence: sources, citations: [], citation_errors: [], plan: [], completed_steps: [], current_step_index: 0,
      research_data: {}, react_logs: [], draft_content: markdown, final_report: markdown, error: null,
      review_history: [], review_comments: [], iteration: 0, max_iterations: 3, passed: !unsafe, forced_pass: false,
      memory_hits: [], created_at: '', updated_at: '' }
    await page.route('**/*', async route => {
      if (new URL(route.request().url()).origin === 'http://127.0.0.1:5175') await route.continue()
      else { unexpected.push(route.request().url()); await route.abort() }
    })
    await page.route('**/api/v*/**', async route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/v2/auth/session') await route.fulfill({ json: { data: { role: 'admin' }, meta: { request_id: 'fixture' } } })
      else if (path === '/api/v1/tasks/source-fixture/status') await route.fulfill({ json: task })
      else if (path === '/api/v1/tasks/source-fixture/report') await route.fulfill({ json: { ...task, sources, title: task.topic,
        content_markdown: markdown, charts: [], executive_summary: { total_time: 1, react_loops: 1, memory_hit_count: 0 } } })
      else { unexpected.push(path); await route.fulfill({ status: 500, json: { detail: '未声明请求' } }) }
    })
    for (const path of ['/tasks/source-fixture', '/report/source-fixture']) {
      await page.goto(path)
      await page.getByLabel('访问令牌').fill('fixture-source-browser')
      await page.getByRole('button', { name: '验证并进入' }).click()
      await page.locator('a.citation-link').first().click()
      await expect(page).toHaveURL(/#source-S1$/)
      await expect(page.locator('#source-S1')).toContainText('原始证据片段')
      await expect(page.locator('#source-S1 a')).toHaveAttribute('href', 'https://example.invalid/evidence')
      await expect(page.locator('#source-S1 a')).toHaveAttribute('rel', 'noopener noreferrer')
      if (unsafe) {
        await expect(page.locator('.citation-invalid').filter({ hasText: 'S999' })).toBeVisible()
        await expect(page.locator('a[href="#source-S2"], a[href^="javascript:"]')).toHaveCount(0)
        await expect(page.locator('.report-content img, .report-content script')).toHaveCount(0)
        expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).__sourceInjected)).toBeUndefined()
      }
    }
    if (!unsafe) {
      const wordEvent = page.waitForEvent('download')
      await page.getByRole('button', { name: '导出 Word', exact: true }).click()
      const word = await wordEvent
      const html = await readFile((await word.path())!, 'utf8')
      expect(html).toContain('source-S1')
      expect(html).toContain('https://example.invalid/evidence')
      expect(html).toContain('原始证据片段')
      expect(html).toContain('不代表事实已核实')
      const pdfEvent = page.waitForEvent('download')
      await page.getByRole('button', { name: '导出 PDF', exact: true }).click()
      const pdf = await pdfEvent
      const bytes = await readFile((await pdf.path())!)
      expect(bytes.subarray(0, 5).toString()).toBe('%PDF-')
      expect(bytes.byteLength).toBeGreaterThan(2000)
    }
    expect(unexpected).toEqual([])
  })
}
