import { test, expect } from '@playwright/test'
import { readFile } from 'node:fs/promises'

// 明确HTTP协议夹具；真实上传/检索/图/仓储见后端集成测试。
test('选择上传资料，详情与报告展示检索片段，Word保留文档溯源', async ({ page }) => {
  const id = 'a'.repeat(32)
  let submitted: unknown
  const unexpected: string[] = []
  const evidence = [{ source_id: 'S1', task_id: 'document-fixture', kind: 'document', document_id: id,
    chunk_id: 'chunk-137', page: 2, score: 0.95, title: '独有事实.txt', excerpt: '蓝鲸交付137台设备。',
    retrieved_at: '2026-10-20T00:00:00Z', url: null, source_deleted: false }]
  const retrievals = [{ task_id: 'document-fixture', document_ids: [id], query: '蓝鲸项目',
    options: { top_k: 5, threshold: 0.35, context_budget: 6000 }, status: 'matched', evidence, error_code: null }]
  const task = { task_id: 'document-fixture', topic: '蓝鲸项目', status: 'completed', execution_status: 'completed',
    quality_status: 'passed', forced_output: false, error_code: null, document_ids: [id], retrievals,
    evidence, citations: [], plan: [], completed_steps: [], current_step_index: 0, research_data: {},
    react_logs: [], draft_content: '蓝鲸交付137台设备。[S1]', final_report: '蓝鲸交付137台设备。[S1]',
    error: null, review_history: [], review_comments: [], iteration: 0, max_iterations: 3, memory_hits: [], created_at: '', updated_at: '' }
  await page.route('**/*', async route => {
    if (new URL(route.request().url()).origin === 'http://127.0.0.1:5175') await route.continue()
    else { unexpected.push(route.request().url()); await route.abort() }
  })
  await page.route('**/api/v*/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v2/auth/session') await route.fulfill({ json: { data: { role: 'admin' }, meta: { request_id: 'fixture' } } })
    else if (path === '/api/v1/knowledge/documents') await route.fulfill({ json: { documents: [
      { document_id: id, source: '独有事实.txt', category: 'upload', chunk_count: 1 },
      { document_id: 'b'.repeat(32), source: '模板.md', category: 'template', chunk_count: 1 },
      { source: '旧资料.txt', category: 'upload', chunk_count: 1 }
    ] } })
    else if (path === '/api/v1/generate') {
      submitted = route.request().postDataJSON()
      await route.fulfill({ json: { task_id: task.task_id, status: 'running', topic: task.topic } })
    } else if (path.endsWith('/status')) await route.fulfill({ json: task })
    else if (path.endsWith('/report')) await route.fulfill({ json: { ...task, sources: evidence, title: task.topic,
      content_markdown: task.final_report, charts: [], executive_summary: { total_time: 1, react_loops: 1, memory_hit_count: 0 } } })
    else { unexpected.push(path); await route.fulfill({ status: 500 }) }
  })
  await page.goto('/home')
  await page.getByLabel('访问令牌').fill('fixture-documents')
  await page.getByRole('button', { name: '验证并进入' }).click()
  await page.locator('.el-select').click()
  await expect(page.locator('.el-select-dropdown__item')).toHaveCount(1)
  await page.locator('.el-select-dropdown__item').click()
  await page.getByRole('textbox').fill('蓝鲸项目')
  await page.getByRole('button', { name: '生成研究报告' }).click()
  await expect(page).toHaveURL(/\/tasks\/document-fixture$/)
  expect(submitted).toEqual({ topic: '蓝鲸项目', document_ids: [id] })
  await expect(page.locator('.retrieval-panel')).toContainText('已找到可用片段')
  await expect(page.locator('.retrieval-panel')).toContainText('页码 2')
  await page.goto('/report/document-fixture')
  await page.getByLabel('访问令牌').fill('fixture-documents')
  await page.getByRole('button', { name: '验证并进入' }).click()
  await expect(page.locator('.retrieval-panel')).toContainText('0.9500')
  await page.locator('a.citation-link').click()
  await expect(page.locator('#source-S1')).toContainText('chunk-137')
  const downloadEvent = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出 Word', exact: true }).click()
  const html = await readFile((await (await downloadEvent).path())!, 'utf8')
  expect(html).toContain(id)
  expect(html).toContain('chunk-137')
  expect(html).toContain('137台设备')
  expect(unexpected).toEqual([])
})

for (const status of ['embedding_failed', 'documents_missing', 'matched']) {
  test(`检索解释区分${status}并标记已删除片段`, async ({ page }) => {
    const source = { source_id: 'S1', task_id: 'doc-error', kind: 'document', document_id: 'a'.repeat(32),
      chunk_id: 'old-chunk', page: null, score: 0.9, title: '<script>恶意标题</script>',
      excerpt: '<img src=x onerror="window.__docInjected=1">历史片段137台',
      retrieved_at: '2026-10-20T00:00:00Z', url: null, source_deleted: true }
    const evidence = status === 'matched' ? [source] : []
    const unexpected: string[] = []
    await page.route('**/*', async route => {
      if (new URL(route.request().url()).origin === 'http://127.0.0.1:5175') await route.continue()
      else { unexpected.push(route.request().url()); await route.abort() }
    })
    await page.route('**/api/v*/**', async route => {
      if (route.request().url().endsWith('/auth/session')) await route.fulfill({ json: { data: { role: 'admin' }, meta: { request_id: 'fixture' } } })
      else await route.fulfill({ json: { task_id: 'doc-error', title: '文档异常', content_markdown: '待补充[S1]',
        sources: evidence, citations: [], document_ids: ['a'.repeat(32)],
        retrievals: [{ task_id: 'doc-error', document_ids: ['a'.repeat(32)], query: '资料', status, evidence,
          options: { top_k: 5, threshold: 0.35, context_budget: 6000 }, error_code: status === 'matched' ? null : 'FIXTURE' }],
        execution_status: 'completed', quality_status: 'rejected', forced_output: true, error_code: 'CITATION_INVALID',
        charts: [], executive_summary: { total_time: 1, react_loops: 1, memory_hit_count: 0 } } })
    })
    await page.goto('/report/doc-error')
    await page.getByLabel('访问令牌').fill('fixture-documents')
    await page.getByRole('button', { name: '验证并进入' }).click()
    await expect(page.locator('.retrieval-panel')).toContainText(status === 'embedding_failed' ? '嵌入服务失败' : status === 'documents_missing' ? '已删除或不存在' : '来源已删除')
    await expect(page.locator('a.citation-link')).toHaveCount(0)
    await expect(page.locator('.retrieval-panel img, .retrieval-panel script, .source-card script, .source-card img')).toHaveCount(0)
    expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).__docInjected)).toBeUndefined()
    expect(unexpected).toEqual([])
  })
}

test('知识库上传后按标识查看与删除同名文档', async ({ page }) => {
  const id = 'a'.repeat(32), other = 'b'.repeat(32)
  let uploaded = false, deleted = false
  const requests: string[] = []
  await page.route('**/api/v*/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    if (path.endsWith('/auth/session')) await route.fulfill({ json: { data: { role: 'admin' }, meta: { request_id: 'fixture' } } })
    else if (path === '/api/v1/memory/status') await route.fulfill({ json: { enabled: false, count: 0 } })
    else if (path === '/api/v1/knowledge/documents' && request.method() === 'POST') {
      uploaded = true
      expect(request.postDataBuffer()?.toString()).toContain('137台')
      await route.fulfill({ json: { document_id: id, source: '资料.txt', display_name: '资料.txt', category: 'upload', added: 1, chunks: 1 } })
    } else if (path === '/api/v1/knowledge/documents') {
      await route.fulfill({ json: { documents: [
        { document_id: other, source: '资料.txt', category: 'upload', chunk_count: 1, file_type: 'txt', snippet: '另一文件' },
        ...(uploaded && !deleted ? [{ document_id: id, source: '资料.txt', category: 'upload', chunk_count: 1, file_type: 'txt', snippet: '137台' }] : [])
      ] } })
    } else if (path === `/api/v2/knowledge/documents/${id}` && request.method() === 'GET') {
      requests.push('view:' + id)
      await route.fulfill({ json: { data: { source: '资料.txt', category: 'upload', document_id: id,
        chunks: [{ id: 'chunk', content: '独有事实137台', metadata: { document_id: id, page_number: 2 } }] }, meta: { request_id: 'fixture' } } })
    } else if (path === `/api/v2/knowledge/documents/${id}` && request.method() === 'DELETE') {
      requests.push('delete:' + id); deleted = true
      await route.fulfill({ json: { data: { deleted: 1 }, meta: { request_id: 'fixture' } } })
    } else throw new Error('未声明请求：' + path)
  })
  await page.goto('/knowledge')
  await page.getByLabel('访问令牌').fill('fixture-documents')
  await page.getByRole('button', { name: '验证并进入' }).click()
  await page.locator('input[type=file]').setInputFiles({ name: '资料.txt', mimeType: 'text/plain', buffer: Buffer.from('独有事实137台') })
  const row = page.locator('.el-table__body tr').filter({ hasText: id })
  await expect(row).toBeVisible()
  await row.getByRole('button', { name: '查看' }).click()
  await expect(page.getByRole('dialog')).toContainText('独有事实137台')
  await page.getByRole('dialog').getByRole('button', { name: 'Close this dialog' }).click()
  await row.getByRole('button', { name: '删除', exact: true }).click()
  await page.getByRole('button', { name: '删除', exact: true }).last().click()
  await expect(row).toHaveCount(0)
  await expect(page.locator('.el-table__body tr').filter({ hasText: other })).toBeVisible()
  expect(requests).toEqual(['view:' + id, 'delete:' + id])
})
