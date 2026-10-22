/**
 * 从 Vite 构建 manifest 计算首页初始 JS 体积。
 *
 * 口径：入口 index.html 与 /home 路由组件的静态 import 闭包内所有 .js 文件。
 * gzip 为本地 zlib 压缩后大小，只用于同机器、同构建条件的相对对照，
 * 不代表真实网络传输、CDN 压缩或首屏时间。
 */
import { existsSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { gzipSync } from 'node:zlib'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const manifestPath = path.join(root, 'dist', '.vite', 'manifest.json')
const entryKey = 'index.html'
const homeKey = 'src/views/Home.vue'

if (!existsSync(manifestPath)) {
  console.error(`缺少 ${path.relative(root, manifestPath)}；请先运行 npm run build（需启用 build.manifest）。`)
  process.exit(1)
}

const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))

function collectStatic(key, seen = new Set()) {
  if (!manifest[key] || seen.has(key)) return seen
  seen.add(key)
  for (const dependency of manifest[key].imports ?? []) collectStatic(dependency, seen)
  return seen
}

const keys = new Set([...collectStatic(entryKey), ...collectStatic(homeKey)])
const jsFiles = new Set()
for (const key of keys) {
  const file = manifest[key]?.file
  if (file && file.endsWith('.js')) jsFiles.add(file)
}

if (jsFiles.size === 0) {
  console.error('manifest 中未找到入口或 /home 路由的 JS 产物。')
  process.exit(1)
}

const files = [...jsFiles].sort().map((file) => {
  const absolute = path.join(root, 'dist', file)
  const raw = statSync(absolute).size
  const gzip = gzipSync(readFileSync(absolute)).length
  return { file, raw, gzip }
})
const total = files.reduce((sum, item) => ({ raw: sum.raw + item.raw, gzip: sum.gzip + item.gzip }), { raw: 0, gzip: 0 })

console.log(JSON.stringify({
  measurement: 'manifest static-import closure for index.html and src/views/Home.vue',
  entry: entryKey,
  route: homeKey,
  files,
  // Vite/Rollup 的 kB 为十进制（1000 字节），这里保持一致以便直接对照构建输出。
  total_kb: { raw: +(total.raw / 1000).toFixed(2), gzip: +(total.gzip / 1000).toFixed(2) },
  total_bytes: total
}, null, 2))
