import { test, expect } from '@playwright/test'

for (const width of [1280, 1024, 768, 390]) {
  test(`运行评测五板块在${width}px窗口不越框，长地址与确认文案完整`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const env = (data: unknown) => ({ data, meta: { request_id: 'fixture' } })
    await page.route('**/api/v*/**', async route => {
      expect(route.request().method()).toBe('GET')
      const path = new URL(route.request().url()).pathname
      if (path.endsWith('/auth/session')) return route.fulfill({ json: env({ role: 'admin' }) })
      if (path.endsWith('/live-config')) return route.fulfill({ json: env({ dataset_hash: 'a'.repeat(64), model: { ready: true, name: 'qwen-long-model-name-for-layout', base_url: 'https://' + 'long-endpoint-'.repeat(8) + '.example.invalid/compatible-mode/v1' }, judge: { ready: true, name: 'qwen-long-model-name-for-layout', base_url: 'https://judge.example.invalid/compatible-mode/v1', same_as_primary: true }, search: { ready: true, base_url: 'https://api.exa.ai/search' } }) })
      if (path.endsWith('/catalog')) return route.fulfill({ json: env({ version: 'v1', cutoff: '2026-11-02', total_cases: 108, cases: [], areas: [
        { area: 'protocol', label: '流程与故障', profile: 'protocol-1', runnable: true },
        { area: 'search', label: '网络搜索', profile: 'search_replay', supported_case_ids: ['G001'], live_case_ids: ['G001'], runnable: true },
        { area: 'report', label: '报告质量', profile: 'report_grading', supported_case_ids: ['G002'], runnable: true },
        { area: 'robustness', label: '鲁棒性与恢复', profile: 'mechanism_regression', supported_case_ids: ['R009', 'G023'], runnable: true },
        { area: 'end_to_end', label: '完整任务', profile: 'full_task_local', supported_case_ids: ['G017'], runnable: true }
      ] }) })
      return route.fulfill({ status: 404, json: { error: { message: '未声明请求' } } })
    })
    await page.goto('/evaluation/run')
    await page.getByLabel('访问令牌').fill('fixture-layout')
    await page.getByRole('button', { name: '验证并进入' }).click()
    await expect(page.getByRole('region', { name: '真实模型评测设置' })).toContainText('qwen-long-model-name-for-layout')
    const overflow = await page.locator('.evaluation-center').evaluate(root => {
      const bad: string[] = []
      for (const node of root.querySelectorAll('article, fieldset, .live-launch, .el-checkbox__label, button')) {
        const e = node as HTMLElement
        if (!e.getBoundingClientRect().width) continue
        if (e.scrollWidth > e.clientWidth + 2) bad.push(`${e.tagName}.${e.className}: ${e.scrollWidth}/${e.clientWidth}`)
      }
      return bad
    })
    expect(overflow).toEqual([])
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    const bounds = await page.locator('.evaluation-center .areas > article').evaluateAll(nodes => nodes.map(n => ({ left: n.getBoundingClientRect().left, right: n.getBoundingClientRect().right })))
    expect(bounds).toHaveLength(5)
    for (const b of bounds) { expect(b.left).toBeGreaterThanOrEqual(0); expect(b.right).toBeLessThanOrEqual(width) }
    await page.screenshot({ path: `../docs/verification/EVAL-LAYOUT-01-${width}.png`, fullPage: true })
  })
}
