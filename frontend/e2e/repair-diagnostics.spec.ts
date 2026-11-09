import { test, expect } from '@playwright/test'

test('审核故障、疑点和实际修改分别展示，刷新后仍不把争议结束显示为通过', async ({ page }) => {
  await page.route('**/api/v*/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/auth/session')) await route.fulfill({ json: { data: { role: 'readonly' }, meta: {} } })
    else if (path.endsWith('/status')) await route.fulfill({ json: {
      task_id: 'repair-diagnostics', topic: '市场研究修复诊断', status: 'completed', execution_status: 'completed',
      quality_status: 'unavailable', forced_output: true, error_code: 'BUDGET_EXCEEDED',
      plan: [], completed_steps: [], current_step_index: 0, research_data: {}, react_logs: [],
      draft_content: '# 市场报告', final_report: '# 市场报告', review_comments: ['核验尚未完成，已保留当前报告。'],
      iteration: 1, max_iterations: 3, memory_hits: [], created_at: '', updated_at: '',
      review_history: [{ round: 1, passed: true, quality_status: 'unavailable', review_complete: false,
        comments: ['核验尚未完成，已保留当前报告。'], stop_reason: 'BUDGET_EXCEEDED',
        review_diagnostics: { code: 'REVIEW_JSON_INVALID', message: '原始响应格式错误' },
        findings: [
          { finding_id: 'A', decision: 'confirmed', severity: 'hard', material: true, problem: '年份冲突' },
          { finding_id: 'B', decision: 'unresolved', severity: 'soft', material: true, problem: '排名待核验' },
          { finding_id: 'C', decision: 'dismissed', severity: 'hard', material: false, problem: '已驳回' },
        ],
        dispute_review: { status: 'reviewed', initial_findings: ['<img src=x onerror=alert(1)>'] },
        content_checks: { core_status: 'unknown', core_coverage: [], semantic_duplicates: [], review_scope: 'full_report' },
        rewrite: { mode: 'document_actions', changed: true, attempts: [{ mode: 'document_actions', decision: 'applied', raw_response: '<script>不执行原文</script>' }], failures: [] },
      }],
    } })
    else if (path.endsWith('/usage')) await route.fulfill({ json: { data: { calls: [], totals: {} }, meta: {} } })
    else await route.fulfill({ json: { data: {}, meta: {} } })
  })
  async function enter() {
    await page.getByLabel('访问令牌').fill('fixture-diagnostics')
    await page.getByRole('button', { name: '验证并进入' }).click()
    await expect(page.locator('.review-round-header .el-tag')).toHaveText('审核不可用')
  }
  await page.goto('/tasks/repair-diagnostics')
  await enter()
  await expect(page.getByTestId('quality-status').first()).toHaveText('审核不可用')
  const summary = page.getByTestId('review-summary')
  await expect(summary).toContainText('已确认问题 1 项')
  await expect(summary).toContainText('待核验疑点 1 项')
  await expect(summary).toContainText('审核响应不可用')
  await expect(summary).toContainText('保留已有报告')
  await expect(summary).toContainText('整稿质量仍以审核结果为准')
  await expect(summary).not.toContainText('BUDGET_EXCEEDED')
  await expect(page.getByTestId('review-technical').locator('pre')).not.toBeVisible()
  const audit = page.getByTestId('rewrite-audit')
  await audit.locator(':scope > summary').click()
  await expect(audit).toContainText('仍需整稿复审')
  await summary.scrollIntoViewIfNeeded()
  await page.screenshot({ path: '../docs/verification/R05-03-diagnostics.png', fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy()
  await page.screenshot({ path: '../docs/verification/R05-03-diagnostics-mobile.png', fullPage: true })
  await page.getByTestId('review-technical').locator('summary').click()
  await expect(page.getByTestId('review-technical')).toContainText('REVIEW_JSON_INVALID')
  await expect(page.getByTestId('review-technical').locator('img,script')).toHaveCount(0)
  await page.reload()
  await enter()
  await expect(summary).toContainText('待核验疑点 1 项')
})
