import { test, expect } from '@playwright/test'

test('任务详情展示程序拦截原因与原始提案，刷新后仍可追溯', async ({ page }) => {
  // HTTP协议夹具；后端仓储持久化由test_rewrite_restrictions验证。
  await page.route('**/api/v*/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/auth/session')) {
      await route.fulfill({ json: { data: { role: 'readonly' }, meta: { request_id: 'audit' } } })
    } else if (path.endsWith('/status')) {
      await route.fulfill({ json: {
        task_id: 'rewrite-audit', topic: '返工诊断', status: 'completed', execution_status: 'completed',
        quality_status: 'rejected', forced_output: true, error_code: 'REVIEW_REJECTED',
        plan: [], completed_steps: [], current_step_index: 0, research_data: {}, react_logs: [],
        draft_content: '# 报告', final_report: '# 报告', review_comments: ['结构需重组'],
        review_history: [{ round: 1, passed: false, comments: ['结构需重组'],
          claim_bindings: [{ paragraph: 2, text: '第一季度规模10亿元[S1]', fact_ids: ['F1'], source_ids: ['S1'] }],
          fact_registry: { F1: { source_id: 'S1', quote: '2026年第一季度规模10亿元。' } },
          dispute_review: { status: 'unresolved', initial_findings: [] },
          rewrite: {
          mode: 'patch', changed: true, failures: ['全文响应无效'],
          verification: [{ finding_id: 'BIND-1', reported_status: 'fixed', status: 'unresolved', reason: '数字仍在' }],
          program_restriction: { observed: true, outcome: 'partial_candidate_applied',
            codes: ['SCOPE_RESTRICTED'], quality_effect: 'unknown_until_review' },
          attempts: [{ mode: 'patch', decision: 'applied',
            request: { scope: 'named_paragraphs', allowed_paragraphs: [2], missing_evidence_ids: ['S9'] },
            accepted: [{ paragraph: 2, replacement: '局部修改' }],
            rejected: [{ index: 1, code: 'SCOPE_RESTRICTED', message: '补丁必须指向被点名段落',
              proposal: { paragraph: 3, replacement: '需要合并的重复章节' } }],
            raw_response: '{"patches":["需要合并的重复章节"]}' }],
        } }], iteration: 1, max_iterations: 3, memory_hits: [], created_at: '', updated_at: '',
      } })
    } else if (path.endsWith('/usage')) {
      await route.fulfill({ json: { data: { calls: [], totals: {} }, meta: {} } })
    } else {
      await route.fulfill({ json: { data: {}, meta: {} } })
    }
  })
  await page.goto('/tasks/rewrite-audit')
  await page.getByLabel('访问令牌').fill('fixture-audit')
  await page.getByRole('button', { name: '验证并进入' }).click()
  for (let i = 0; i < 2; i++) {
    const evidence = page.locator('details').filter({ has: page.locator('summary', { hasText: '声明依据与争议复核' }) }).first()
    await evidence.locator(':scope > summary').click()
    await expect(evidence).toContainText('2026年第一季度规模10亿元。')
    await expect(evidence).toContainText('待核验，质量尚未确认')
    const audit = page.getByTestId('rewrite-audit')
    await audit.locator(':scope > summary').click()
    await expect(audit).toContainText('程序是否拦截过提案：是')
    await expect(audit).toContainText('BIND-1：模型自报已修复；实际检查仍未解决')
    await audit.getByText('查看返工技术明细', { exact: true }).click()
    await audit.getByText('第 1 次尝试', { exact: false }).click()
    await expect(audit).toContainText('SCOPE_RESTRICTED')
    await expect(audit).toContainText('需要合并的重复章节')
    await expect(audit).toContainText('S9')
    if (i === 0) {
      await page.reload()
      // 访问令牌按既有产品契约只存内存，刷新后重新认证。
      await page.getByLabel('访问令牌').fill('fixture-audit')
      await page.getByRole('button', { name: '验证并进入' }).click()
    }
  }
})
