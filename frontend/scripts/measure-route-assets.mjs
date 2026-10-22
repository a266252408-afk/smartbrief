/**
 * 计算指定路由的静态 JS 闭包（入口 + 路由静态 import），用于按需加载对照。
 *
 * 只看 manifest 的静态 imports；dynamicImports 表示运行时按需加载，不计入路由到达成本。
 * gzip 为本地 zlib 结果，只用于同机器、同构建条件的相对对照。
 */
import { existsSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { gzipSync } from 'node:zlib'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const manifestPath = path.join(root, 'dist', '.vite', 'manifest.json')
const entryKey = 'index.html'
const defaultRoutes = ['src/views/Evaluation.vue', 'src/views/TaskDetail.vue', 'src/views/ReportView.vue']
const routeKeys = process.argv.slice(2).length ? process.argv.slice(2) : defaultRoutes

if (!existsSync(manifestPath)) {
  console.error(`缺少 ${path.relative(root, manifestPath)}；请先运行 npm run build。`)
  process.exit(1)
}

const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))

function collectStatic(key, seen = new Set()) {
  if (!manifest[key] || seen.has(key)) return seen
  seen.add(key)
  for (const dependency of manifest[key].imports ?? []) collectStatic(dependency, seen)
  return seen
}

const entryKeys = collectStatic(entryKey)
const routes = routeKeys.map((routeKey) => {
  if (!manifest[routeKey]) {
    throw new Error(`manifest 中不存在路由 ${routeKey}`)
  }
  const keys = new Set([...entryKeys, ...collectStatic(routeKey)])
  const jsFiles = new Set()
  for (const key of keys) {
    const file = manifest[key]?.file
    if (file && file.endsWith('.js')) jsFiles.add(file)
  }
  const files = [...jsFiles].sort().map((file) => {
    const raw = statSync(path.join(root, 'dist', file)).size
    const gzip = gzipSync(readFileSync(path.join(root, 'dist', file))).length
    return { file, raw, gzip }
  })
  const total = files.reduce((sum, item) => ({ raw: sum.raw + item.raw, gzip: sum.gzip + item.gzip }), { raw: 0, gzip: 0 })
  return {
    route: routeKey,
    file: manifest[routeKey].file,
    static_dynamic_imports: manifest[routeKey].dynamicImports ?? [],
    files,
    total_bytes: total,
    total_kb: { raw: +(total.raw / 1000).toFixed(2), gzip: +(total.gzip / 1000).toFixed(2) }
  }
})

console.log(JSON.stringify({
  measurement: 'manifest static import closure for entry plus each route',
  routes
}, null, 2))
