import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import MemoryManager from '../MemoryManager.vue'
import { fetchMemories, deleteMemory, type MemoryEntry, type MemoryPage } from '../../api/knowledge'
import { regenerateTask } from '../../api/tasks'
import { authRole } from '../../api/session'

const { push } = vi.hoisted(() => ({ push: vi.fn() }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))
vi.mock('../../api/knowledge', () => ({ fetchMemories: vi.fn(), deleteMemory: vi.fn() }))
vi.mock('../../api/tasks', () => ({ regenerateTask: vi.fn() }))
const entry: MemoryEntry = { memory_id: 'a'.repeat(64), task_id: 'old', topic: '<img src=x onerror=alert(1)>旧结构',
  quality_status: 'unknown', verified: false, status: 'unverified', report_hash: null, model_name: null,
  model_version: null, prompt_version: null, created_at: null, invalid_reason: 'legacy_unverified' }
const page: MemoryPage = { items: [entry], total: 1, page: 1, page_size: 10, enabled: false,
  counts: { active: 0, unverified: 1, unavailable: 0, invalid: 0, deleted: 0 } }
let wrapper: VueWrapper
beforeEach(() => {
  authRole.value = 'admin'
  vi.mocked(fetchMemories).mockResolvedValue(structuredClone(page))
  wrapper = mount(MemoryManager, { global: { plugins: [ElementPlus] } })
})
afterEach(() => { ElMessageBox.close(); wrapper.unmount(); vi.restoreAllMocks(); authRole.value = null })
async function open() { await wrapper.findAll('button')[0].trigger('click'); await flushPromises() }

it('按需读取，旧记录明确未验证与版本未知，开关关闭不冒充清空', async () => {
  expect(fetchMemories).not.toHaveBeenCalled()
  await open()
  expect(wrapper.text()).toContain('历史未验证')
  expect(wrapper.text()).toContain('供应商版本 未知')
  expect(wrapper.text()).toContain('数据仍保留')
  expect(wrapper.find('.memory-table img').exists()).toBe(false)
  const filter = wrapper.findComponent({ name: 'ElSelect' })
  filter.vm.$emit('update:modelValue', 'unverified'); filter.vm.$emit('change', 'unverified')
  await flushPromises()
  expect(fetchMemories).toHaveBeenLastCalledWith(1, 'unverified')
})

it('删除后刷新状态，原报告不由删除记忆接口改写', async () => {
  await open()
  vi.mocked(deleteMemory).mockResolvedValue({ memory_id: entry.memory_id, deleted: true, status: 'deleted' })
  vi.mocked(fetchMemories).mockResolvedValue({ ...page, items: [{ ...entry, status: 'deleted' }] })
  await wrapper.findAll('button').find(b => b.text() === '删除记忆')!.trigger('click')
  await flushPromises()
  const confirm = document.querySelector<HTMLButtonElement>('.el-message-box__btns .el-button--primary')
  expect(confirm).not.toBeNull()
  confirm!.click()
  await flushPromises()
  expect(deleteMemory).toHaveBeenCalledWith(entry.memory_id)
  expect(wrapper.text()).toContain('已删除')
})

it('重新生成后跳到新任务，失败时说明原报告保留', async () => {
  await open()
  vi.mocked(regenerateTask).mockRejectedValueOnce(new Error('fixture'))
  await wrapper.findAll('button').find(b => b.text() === '重新生成')!.trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('原报告保持不变')
  vi.mocked(regenerateTask).mockResolvedValueOnce({ task_id: 'new', topic: '新主题', status: 'running', regenerated_from: 'old' })
  await wrapper.findAll('button').find(b => b.text() === '重新生成')!.trigger('click')
  await flushPromises()
  expect(push).toHaveBeenCalledWith('/tasks/new')
})

it('只读界面禁用删除与重新生成', async () => {
  authRole.value = 'readonly'
  await open()
  for (const name of ['删除记忆', '重新生成']) expect(wrapper.findAll('button').find(b => b.text() === name)!.attributes('disabled')).toBeDefined()
})

it('列表失败明确不可用，不把失败当空库', async () => {
  vi.mocked(fetchMemories).mockRejectedValue(new Error('offline'))
  await open()
  expect(wrapper.text()).toContain('不能据此认定记忆为空')
})
