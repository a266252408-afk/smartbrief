# 前端性能测量（T12）

本文记录首屏资源与页面就绪时间的**本地测量口径和实测结果**，只用于同机器、同构建条件的相对对照。它不是真实网络、CDN 或用户设备性能承诺。

## 1. 测量环境

| 项目 | 值 |
| --- | --- |
| 机器 | AMD Ryzen 5 4600H，12 线程，31.4 GB 内存 |
| 系统 | Windows 10.0.19045 x64 |
| Node | v24.18.0 |
| 构建 | `npm run build`（`vue-tsc --noEmit && vite build`） |
| 预览 | `vite preview`，回环地址 127.0.0.1，无网络限速、无 CDN |

浏览器测量使用仓库内 Playwright Chromium；接口使用离线夹具，不调用后端、模型或付费服务。

## 2. 测量方法

### 2.1 首页初始 JS

脚本 `frontend/scripts/measure-initial-assets.mjs`：

1. 读取 `dist/.vite/manifest.json`（`vite.config.ts` 已启用 `build.manifest`）。
2. 从入口 `index.html` 与当前路由 `src/views/Home.vue` 出发，递归收集 `imports` 中的**静态**依赖。
3. 汇总闭包内所有 `.js` 产物的原始字节与 zlib gzip 字节。

Vite/Rollup 的 kB 使用十进制（1000 字节）；脚本同时输出字节数，避免单位混淆。未进入静态闭包的懒加载页面不计入首页初始 JS。

浏览器交叉核对：`npm run measure:page-ready` 同时记录 `performance` 中实际加载的 `.js` 资源及 `encodedBodySize`，用于确认静态闭包与实际首屏请求一致。

### 2.2 页面就绪时间

脚本 `frontend/scripts/measure-page-ready.mjs`：

1. 启动本地 `vite preview` 加载同一份 `dist`。
2. 认证接口与演示接口使用离线夹具；打开 `/home`，填写固定令牌并进入。
3. 以首页标题“多智能体报告生成”可见为就绪点，读取浏览器 `performance.now()`。
4. 连续运行 5 次取中位数；同时记录 `DOMContentLoaded` 与 `load`。

该时间包含浏览器解析、脚本执行、认证请求往返（本地夹具）与首页渲染，不包含真实网络延迟。

## 3. T11 基线（路由懒加载前）

- 构建产物只有一个入口 JS：3,320.79 kB（gzip 1,060.69 kB），所有页面与 ECharts、html2pdf 均进入首屏公共包。
- 页面就绪中位数 735 ms；DOMContentLoaded 中位数 549 ms；load 中位数 550 ms。
- 证据：[T12-01-baseline-build.txt](verification/T12-01-baseline-build.txt)、[T12-01-baseline-page-ready.json](verification/T12-01-baseline-page-ready.json)。

## 4. T12-01 结果（路由懒加载后）

首页初始 JS 静态闭包：

| 产物 | 原始字节 | gzip 字节 |
| --- | ---: | ---: |
| `index-*.js`（入口） | 1,104,106 | 364,844 |
| `Home-*.js` | 7,983 | 3,600 |
| `tasks-*.js`（Home 共享 API） | 1,231 | 541 |
| `knowledge-*.js`（Home 共享 API） | 1,475 | 520 |
| **合计** | **1,114,795** | **369,505** |

- gzip 传输体积：1,060,692 → 369,505 字节，下降 **65.16%**，超过 AC 要求的 30%。
- 原始字节：3,320,790 → 1,114,795，下降 66.43%。
- 浏览器实际加载的 JS 资源与上述闭包完全一致（见 `T12-01-page-ready.json` 的 `js_resources`）。
- 页面就绪中位数 735 → 294 ms；DOMContentLoaded 549 → 124 ms；load 550 → 153 ms。
- 懒加载块失败时显示“页面加载失败：<路径>，请刷新重试”，避免路由空白；浏览器断言见 `frontend/e2e/routing.spec.ts`。
- 评测页 ECharts（约 1,051.66 kB / gzip 350.03 kB）与 html2pdf（约 986.64 kB / gzip 288.34 kB）已移出首屏，但仍分别位于评测、任务详情/报告路由的懒加载块中；导出与图表的进一步按需加载属于 T12-02。
- 证据：[T12-01-initial-assets.json](verification/T12-01-initial-assets.json)、[T12-01-page-ready.json](verification/T12-01-page-ready.json)、[T12-01-frontend-build.txt](verification/T12-01-frontend-build.txt)。

## 5. 复现命令

```text
cd frontend
npm run build
npm run measure:initial-js
npm run measure:page-ready
```

## 6. 限制

- 只测本机回环环境，未模拟 3G/4G、跨地域延迟或 CDN；不同机器、Node 版本与构建条件不能直接比较数值。
- gzip 为本地 zlib 结果，与服务器 Brotli/CDN 的实际传输量可能不同；结论只针对相对变化。
- 页面就绪使用固定认证与演示接口夹具，不代表真实在线模型生成耗时。
- E2E 使用 Vite dev server 验证功能，不用于性能数值；性能数值只来自生产构建。
- 未修改构建告警阈值来“消除”大块提示；T12-02 继续处理图表与导出依赖。
