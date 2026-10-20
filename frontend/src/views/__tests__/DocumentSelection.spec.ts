import { afterEach, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import Home from '../Home.vue'
import { submitTask } from '../../api/tasks'
import { fetchDocuments } from '../../api/knowledge'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('../../api/tasks', () => ({ submitTask: vi.fn() }))
vi.mock('../../api/knowledge', () => ({ fetchDocuments: vi.fn() }))
afterEach(() => vi.restoreAllMocks())

it('只列出有ID的上传资料，提交选取的文档ID', async () => {
  vi.mocked(fetchDocuments).mockResolvedValue([
    { document_id: 'a'.repeat(32), category: 'upload', source: '资料.txt', file_type: 'txt', chunk_count: 1, snippet: '137台设备' },
    { document_id: 'b'.repeat(32), category: 'template', source: '模板.md', file_type: 'md', chunk_count: 1, snippet: '结构' },
    { category: 'upload', source: '旧资料.txt', file_type: 'txt', chunk_count: 1, snippet: '旧内容' }
  ])
  vi.mocked(submitTask).mockResolvedValue({ task_id: 't', status: 'running', topic: 'q' })
  const wrapper = mount(Home, { global: { plugins: [ElementPlus] } })
  try {
    const select = wrapper.findComponent({ name: 'ElSelect' })
    expect(select.exists()).toBe(true)
    select.vm.$emit('visible-change', true)
    await flushPromises()
    expect(wrapper.findAllComponents({ name: 'ElOption' })).toHaveLength(1)
    select.vm.$emit('update:modelValue', ['a'.repeat(32)])
    await wrapper.get('textarea').setValue('q')
    await wrapper.findAll('button').find(b => b.text() === '生成研究报告')!.trigger('click')
    await flushPromises()
    expect(submitTask).toHaveBeenCalledWith('q', ['a'.repeat(32)])
  } finally { wrapper.unmount() }
})
