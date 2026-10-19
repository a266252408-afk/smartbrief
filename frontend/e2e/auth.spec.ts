import { test, expect } from '@playwright/test'

test('无效令牌、只读拒绝写入和401重新认证', async ({ page }) => {
  const unexpected: string[] = []
  await page.route('**/*', async route => {
    if (new URL(route.request().url()).origin === 'http://127.0.0.1:5175') await route.continue()
    else { unexpected.push(route.request().url()); await route.abort() }
  })
  await page.route('**/api/v*/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const token = request.headers()['authorization']
    if (path === '/api/v2/auth/session') {
      await route.fulfill(token === 'Bearer fixture-readonly' ?
        { json: { data: { role:'readonly' }, meta:{ request_id:'fixture' } } } :
        { status:401, json:{ error:{ message:'无效令牌' } } })
    } else if (path === '/api/v1/generate') {
      expect(token).toBe('Bearer fixture-readonly')
      await route.fulfill({ status:403, json:{ detail:'只读令牌无权执行此操作' } })
    } else if (path === '/api/v1/tasks') {
      await route.fulfill({ status:401, json:{ detail:'凭证已失效' } })
    } else { unexpected.push(path); await route.fulfill({ status:500 }) }
  })
  await page.goto('/home')
  await page.getByLabel('访问令牌').fill('invalid-fixture')
  await page.getByRole('button',{name:'验证并进入'}).click()
  await expect(page.getByRole('alert')).toContainText('认证失败')
  await page.getByLabel('访问令牌').fill('fixture-readonly')
  await page.getByRole('button',{name:'验证并进入'}).click()
  await expect(page.getByText('只读访问',{exact:true})).toBeVisible()
  await page.getByRole('textbox').fill('只读测试')
  await page.getByRole('button',{name:'生成研究报告'}).click()
  await expect(page.getByRole('alert').filter({ hasText:'只读访问' })).toBeVisible()
  await expect(page).toHaveURL(/\/home$/)
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0)
  expect(page.url()).not.toContain('fixture-readonly')
  await page.getByRole('link',{name:'任务列表',exact:true}).click()
  await expect(page.getByRole('heading',{name:'访问认证'})).toBeVisible()
  await expect(page.getByRole('alert').filter({ hasText:'重新认证' })).toBeVisible()
  expect(unexpected).toEqual([])
})
