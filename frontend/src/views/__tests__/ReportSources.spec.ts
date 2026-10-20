import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import ReportView from '../ReportView.vue'
import { fetchTaskReport, type TaskReport } from '../../api/tasks'
const pdf = vi.hoisted(() => ({ from: vi.fn(), set: vi.fn(), save: vi.fn() }))
vi.mock('html2pdf.js', () => ({ default: () => pdf }))
vi.mock('vue-router', () => ({ useRoute: () => ({ params: { taskId: 't' } }), useRouter: () => ({ push: vi.fn() }) }))
vi.mock('../../api/tasks', () => ({ fetchTaskReport: vi.fn() }))
let wrapper: VueWrapper
const fixture: TaskReport = {
  task_id: 't', title: '测试报告', content_markdown: '正文声明[S1]。未知来源[S999]。',
  execution_status: 'completed', quality_status: 'rejected', forced_output: false, error_code: 'CITATION_INVALID',
  sources: [{ source_id: 'S1', task_id: 't', kind: 'web', title: '明确来源', excerpt: '原始证据片段',
    url: 'https://example.invalid/evidence', retrieved_at: '2026-10-20T00:00:00Z', source_deleted: false }],
  citations: [{ source_id: 'S1', claim: '正文声明', valid: true, reason: null }, { source_id: 'S999', claim: '未知来源', valid: false, reason: '来源不存在' }],
  citation_errors: ['来源不存在'], charts: [], executive_summary: { total_time: 1, react_loops: 1, memory_hit_count: 0 }
}
beforeEach(async () => {
  pdf.set.mockReturnValue(pdf); pdf.from.mockReturnValue(pdf); pdf.save.mockResolvedValue(undefined)
  vi.mocked(fetchTaskReport).mockResolvedValue(structuredClone(fixture))
  wrapper = mount(ReportView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
})
afterEach(() => { wrapper.unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
describe('报告引用和导出保留来源', () => {
  it('有效编号链接到包含原URL与片段的来源卡片，未知编号不可点击', () => {
    expect(wrapper.get('a[href="#source-S1"]').text()).toBe('[S1]')
    expect(wrapper.get('#source-S1').text()).toContain('原始证据片段')
    expect(wrapper.get('#source-S1 a').attributes('href')).toBe('https://example.invalid/evidence')
    expect(wrapper.get('.citation-invalid').text()).toContain('S999')
    expect(wrapper.find('a[href="#source-S999"]').exists()).toBe(false)
  })
  it('PDF导出输入包含正文、来源编号、URL、片段和校验限制', async () => {
    await wrapper.findAll('button').find(b => b.text() === '导出 PDF')!.trigger('click')
    await flushPromises()
    const input = pdf.from.mock.calls[pdf.from.mock.calls.length - 1][0] as HTMLElement
    expect(input.textContent).toContain('[S1]')
    expect(input.textContent).toContain('原始证据片段')
    expect(input.innerHTML).toContain('https://example.invalid/evidence')
    expect(input.textContent).toContain('不代表事实已核实')
    expect(pdf.save).toHaveBeenCalled()
  })
  it('Word下载的HTML包含来源映射并保持未知引用标记', async () => {
    let output: Blob | undefined
    vi.stubGlobal('URL', class extends URL { static createObjectURL(blob: Blob) { output=blob; return 'blob:fixture' } static revokeObjectURL() {} })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    await wrapper.findAll('button').find(b => b.text() === '导出 Word')!.trigger('click')
    const text = await new Promise<string>(resolve => { const reader=new FileReader(); reader.onload=()=>resolve(String(reader.result)); reader.readAsText(output!) })
    expect(text).toContain('https://example.invalid/evidence')
    expect(text).toContain('原始证据片段')
    expect(text).toContain('source-S1')
    expect(text).toContain('S999')
    vi.unstubAllGlobals()
  })
})
