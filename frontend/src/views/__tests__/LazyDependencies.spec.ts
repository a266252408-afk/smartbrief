import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'

const state = vi.hoisted(() => ({
  html2pdfLoads: 0,
  save: vi.fn(),
  report: {
    task_id: 'lazy-fixture', title: '按需导出报告', content_markdown: '# 正文', sources: [],
    citations: [], quality_status: 'passed', forced_output: false, metadata: {}, charts: [],
    retrievals: [], document_ids: [],
    executive_summary: { total_time: 1, react_loops: 0, memory_hit_count: 0 }
  }
}))

vi.mock('html2pdf.js', () => {
  state.html2pdfLoads += 1
  const chain = {
    set: vi.fn(() => chain),
    from: vi.fn(() => chain),
    save: state.save
  }
  return { default: () => chain }
})
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { taskId: 'lazy-fixture' } }),
  useRouter: () => ({ push: vi.fn() })
}))
vi.mock('../../api/tasks', () => ({
  fetchTaskReport: vi.fn(async () => state.report)
}))

import ReportView from '../ReportView.vue'

describe('导出依赖按需加载', () => {
  it('打开报告页不加载 html2pdf，点击导出后才加载并执行', async () => {
    const wrapper = mount(ReportView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(state.html2pdfLoads).toBe(0)

    await wrapper.findAll('button').find((button) => button.text() === '导出 PDF')!.trigger('click')
    await flushPromises()

    expect(state.html2pdfLoads).toBe(1)
    expect(state.save).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})
