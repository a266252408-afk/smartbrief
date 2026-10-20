import { describe, expect, it } from 'vitest'
import { renderReport, safeSourceUrl } from '../report'
import type { Evidence } from '../../api/tasks'
const source: Evidence = { task_id: 't', source_id: 'S1', kind: 'web', title: '来源', excerpt: '证据片段', url: 'https://example.invalid/a', retrieved_at: '2026-10-20T00:00:00Z', source_deleted: false }
function doc(html: string) { return new DOMParser().parseFromString(html, 'text/html') }
describe('来源HTML安全边界', () => {
  it.each(['javascript:alert(1)','data:text/html,x','//example.invalid/a','https://user:secret@example.invalid','https://@example.invalid/a','https://example.invalid/\\a','https://exam ple.invalid'])('拒绝不安全来源URL %s', value => {
    expect(safeSourceUrl(value)).toBeNull()
    const result=doc(renderReport('声明[S1]',[{...source,url:value}],'t'))
    expect(result.querySelector('a.citation-link')).toBeNull()
    expect(result.querySelector('#source-S1')?.textContent).toContain('链接不可用')
  })
  it('来源标题/片段和模型HTML不能执行或伪造来源锚点', () => {
    const result=doc(renderReport('<div id="source-S1">伪造锚点</div>\n\n声明[S1]', [{...source,title:'<img src=x onerror=alert(1)>',excerpt:'<script>alert(1)</script>'}],'t'))
    expect(result.querySelectorAll('#source-S1')).toHaveLength(1)
    expect(result.querySelector('img,script,[onerror]')).toBeNull()
    expect(result.body.textContent).toContain('<script>alert(1)</script>')
  })
  it('不展示其他任务来源，已删除或重复来源不可作为有效引用', () => {
    expect(doc(renderReport('声明[S1]',[{...source,task_id:'other'}],'t')).querySelector('#source-S1')).toBeNull()
    for (const sources of [[{...source,source_deleted:true}],[source,source]]) {
      expect(doc(renderReport('声明[S1]',sources,'t')).querySelector('a.citation-link')).toBeNull()
    }
  })
  it('代码中的标记不链接，普通相邻引用仍分别可追溯', () => {
    const result=doc(renderReport('`[S1]`\n\n声明[S1][S2]',[source,{...source,source_id:'S2',url:'https://example.invalid/b'}],'t'))
    expect(result.querySelectorAll('a.citation-link')).toHaveLength(2)
    expect(result.querySelector('code a')).toBeNull()
  })
  it('文档来源无需外链，失配声明与危险Markdown链接不可点击', () => {
    const local={...source,kind:'document' as const,url:null,document_id:'d',chunk_id:'c'}
    const result=doc(renderReport('声明[S1] [危险](javascript:alert(1))',[local],'t',[{source_id:'S1',claim:'声明',valid:false,reason:'无效'}]))
    expect(result.querySelector('a.citation-link,a[href^="javascript:"]')).toBeNull()
    expect(doc(renderReport('声明[S1]',[local],'t')).querySelector('a.citation-link')).not.toBeNull()
  })
})
