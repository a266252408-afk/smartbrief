/**
 * 本地生产构建的首页就绪时间测量。
 *
 * 同一台机器、同一 dist、同样离线 API 夹具下运行 5 次，取中位数。
 * “就绪”定义为认证后首页标题可见；时间来自浏览器 performance.now()，
 * 只反映本地静态预览，不虚构真实网络或用户设备性能。
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const port = 5176
const baseURL = `http://127.0.0.1:${port}`
const viteBin = path.join(root, 'node_modules', 'vite', 'bin', 'vite.js')

if (!existsSync(path.join(root, 'dist', 'index.html'))) {
  console.error('缺少 dist/index.html；请先运行 npm run build。')
  process.exit(1)
}

process.env.PLAYWRIGHT_BROWSERS_PATH ??= fileURLToPath(new URL('../.browser-cache', import.meta.url))
const { chromium } = await import('@playwright/test')

const server = spawn(process.execPath, [viteBin, 'preview', '--host', '127.0.0.1', '--port', String(port), '--strictPort'], {
  cwd: root,
  stdio: 'ignore',
  windowsHide: true
})

async function waitForServer() {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const response = await fetch(baseURL)
      if (response.ok) return
    } catch {
      // 服务尚未就绪，继续等待。
    }
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
  throw new Error('vite preview 未在 20 秒内就绪')
}

function mockApi(route) {
  const url = new URL(route.request().url())
  if (url.pathname === '/api/v2/auth/session') {
    return route.fulfill({ json: { data: { role: 'admin' }, meta: { request_id: 'perf' } } })
  }
  if (url.pathname === '/api/v2/demo/environment') {
    return route.fulfill({ json: { data: { demo_available: true, demo_count: 0, llm_configured: false,
      embedding_configured: false, execution_mode: 'offline', message: '性能测量夹具' }, meta: { request_id: 'perf' } } })
  }
  if (url.pathname === '/api/v2/demo/samples') {
    return route.fulfill({ json: { data: { items: [] }, meta: { request_id: 'perf' } } })
  }
  return route.fulfill({ status: 500, json: { detail: '性能测量未声明的请求' } })
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b)
  return sorted[Math.floor(sorted.length / 2)]
}

let browser
try {
  await waitForServer()
  browser = await chromium.launch()
  const samples = []
  for (let run = 0; run < 5; run += 1) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    const page = await context.newPage()
    await page.route('**/api/**', mockApi)
    await page.goto(`${baseURL}/home`, { waitUntil: 'domcontentloaded' })
    await page.getByLabel('访问令牌').fill('perf-admin')
    await page.getByRole('button', { name: '验证并进入' }).click()
    await page.getByRole('heading', { name: '多智能体报告生成' }).waitFor({ state: 'visible' })
    samples.push(await page.evaluate(() => {
      const navigation = performance.getEntriesByType('navigation')[0]
      return {
        ready_ms: Math.round(performance.now()),
        dom_content_loaded_ms: Math.round(navigation.domContentLoadedEventEnd),
        load_ms: Math.round(navigation.loadEventEnd),
        js_resources: performance.getEntriesByType('resource')
          .filter((entry) => entry.name.endsWith('.js'))
          .map((entry) => ({ name: entry.name.split('/').pop(), encoded_body_size: entry.encodedBodySize }))
      }
    }))
    await context.close()
  }
  const cpus = os.cpus()
  console.log(JSON.stringify({
    measurement: 'local vite preview, offline API fixtures, authentication then /home heading visible',
    environment: {
      platform: `${os.platform()} ${os.release()}`,
      architecture: os.arch(),
      cpu: cpus[0]?.model ?? 'unknown',
      cpu_count: cpus.length,
      memory_gb: +(os.totalmem() / 1024 ** 3).toFixed(1),
      node: process.version,
      runs: samples.length
    },
    samples,
    median: {
      ready_ms: median(samples.map((sample) => sample.ready_ms)),
      dom_content_loaded_ms: median(samples.map((sample) => sample.dom_content_loaded_ms)),
      load_ms: median(samples.map((sample) => sample.load_ms))
    }
  }, null, 2))
} finally {
  await browser?.close()
  server.kill()
}
