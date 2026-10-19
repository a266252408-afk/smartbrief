import { defineConfig } from '@playwright/test'
import { fileURLToPath } from 'node:url'

// 浏览器缓存固定在项目内，安装与执行使用同一路径。
process.env.PLAYWRIGHT_BROWSERS_PATH ??= fileURLToPath(new URL('./.browser-cache', import.meta.url))

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:5175', headless: true, trace: 'retain-on-failure' },
  projects: [{ name: 'chromium', use: { browserName: 'chromium', viewport: { width: 1440, height: 1000 } } }],
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5175 --strictPort',
    url: 'http://127.0.0.1:5175',
    reuseExistingServer: false,
    timeout: 60000
  }
})
