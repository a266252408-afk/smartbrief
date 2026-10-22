import { test, expect } from '@playwright/test'

test('懒加载页面块失败时提示刷新，不留下空白路由', async ({ page }) => {
  // 开发服务器按源文件路径提供懒加载模块；中断该请求模拟部署后旧页面块不可用。
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/src/views/Home.vue') {
      await route.abort()
      return
    }
    if (url.pathname.startsWith('/api/')) {
      await route.fulfill({ status: 500, json: { detail: '错误边界夹具' } })
      return
    }
    await route.continue()
  })

  await page.goto('/home')

  const alert = page.getByRole('alert').filter({ hasText: '页面加载失败' })
  await expect(alert).toBeVisible()
  await expect(alert).toContainText('/home')
  await expect(page.getByRole('button', { name: '刷新页面' })).toBeVisible()
})
