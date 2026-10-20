import { Marked } from 'marked'
import DOMPurify from 'dompurify'
import type { Citation, Evidence, QualityStatus } from '../api/tasks'
import { qualityInfo } from './quality'

export function escapeHtml(value: unknown): string {
  return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!))
}

/** 不接受相对链接、凭证、反斜杠与控制字符，避免浏览器URL修正规则造成绕过。 */
export function safeSourceUrl(value: unknown): string | null {
  if (typeof value !== 'string' || /[\\\s\u0000-\u001f]/.test(value) || /^https?:\/\/[^/]*@/i.test(value)) return null
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && !!url.hostname && !url.username && !url.password ? url.href : null
  } catch { return null }
}

/** 显示、Word与PDF共用同一净化结果；引用有效不代表事实已核实。 */
export function renderReport(markdown: string, sources: Evidence[] = [], taskId: string, citations: Citation[] = [], quality?: QualityStatus): string {
  const scoped = sources.filter(s => s.task_id === taskId && /^S[1-9][0-9]*$/.test(s.source_id))
  const known = new Map(scoped.map(s => [s.source_id, s]))
  const duplicates = new Set(scoped.filter((s, i) => scoped.findIndex(x => x.source_id === s.source_id) !== i).map(s => s.source_id))
  const invalid = new Set(citations.filter(c => !c.valid).map(c => c.source_id))
  function active(id: string) {
    const source = known.get(id)
    return source && !duplicates.has(id) && !invalid.has(id) && !source.source_deleted && !!source.excerpt?.trim() &&
      (source.kind === 'web' ? !!safeSourceUrl(source.url) : !!source.document_id && !!source.chunk_id)
  }
  const parser = new Marked({ renderer: { html: token => escapeHtml(token.text) }, extensions: [{
    name: 'sourceCitation', level: 'inline', start: text => text.indexOf('[S'),
    tokenizer(text) { const m = /^\[(S[0-9]+)\]/.exec(text); if (m) return { type: 'sourceCitation', raw: m[0], sourceId: m[1] } },
    renderer(token) {
      const id = String(token.sourceId)
      return active(id) ? `<a class="citation-link" href="#source-${id}" aria-label="查看来源${id}">[${id}]</a>` :
        `<span class="citation-invalid" role="note">[${id}]（来源无效）</span>`
    }
  }] })
  const body = parser.parse(markdown, { async: false })
  const cards = [...known.values()].map(s => {
    const url = safeSourceUrl(s.url)
    const link = url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a>` : '<span>链接不可用</span>'
    return `<article class="source-card" id="source-${s.source_id}"><h4>[${s.source_id}] ${escapeHtml(s.title)}</h4>` +
      `${active(s.source_id) ? '' : '<p class="citation-invalid">来源无效或已失效</p>'}` +
      (s.kind === 'document' ? `<p>文档：${escapeHtml(s.document_id)} · 片段：${escapeHtml(s.chunk_id)} · 页码：${escapeHtml(s.page ?? '不可获得')} · 相似度：${escapeHtml(s.score ?? '未知')}</p>` : `<p>原始URL：${link}</p>`) +
      (s.source_deleted ? '<p class="citation-invalid">来源已删除，保留历史证据片段</p>' : '') +
      `<blockquote>${escapeHtml(s.excerpt)}</blockquote>` +
      `<p>收集时间：${escapeHtml(s.retrieved_at)}</p></article>`
  }).join('')
  const appendix = `<section class="source-appendix"><h3>来源与证据片段</h3><p>引用检查仅验证来源映射，不代表事实已核实。</p>${cards || '<p>暂无可追溯来源，数据待补充。</p>'}</section>`
  const html = DOMPurify.sanitize(`<p class="report-quality">报告质量：${qualityInfo(quality).label}</p>${body}${appendix}`, {
    ADD_ATTR: ['target'], FORBID_TAGS: ['script', 'style', 'iframe', 'object', 'embed', 'img']
  })
  const doc = new DOMParser().parseFromString(html, 'text/html')
  // 普通Markdown链接也适用相同的外链协议限制。
  for (const anchor of doc.querySelectorAll('a[href]')) {
    const href = anchor.getAttribute('href') ?? ''
    if (/^#[a-zA-Z0-9_-]+$/.test(href)) continue
    const safe = safeSourceUrl(href)
    if (!safe) { anchor.removeAttribute('href'); continue }
    anchor.setAttribute('href', safe)
    anchor.setAttribute('target', '_blank')
    anchor.setAttribute('rel', 'noopener noreferrer')
  }
  return doc.body.innerHTML
}
