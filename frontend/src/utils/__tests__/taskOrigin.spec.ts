import { expect, it } from 'vitest'
import { isOfflineTask } from '../taskOrigin'

it('显式模式优先并兼容旧离线主题', () => {
  expect(isOfflineTask({ topic: '普通', metadata: { execution_mode: 'offline' } })).toBe(true)
  expect(isOfflineTask({ topic: '[离线协议] 旧记录' })).toBe(true)
  expect(isOfflineTask({ topic: '[离线协议] 用户主题', metadata: { execution_mode: 'online' } })).toBe(false)
  expect(isOfflineTask({ topic: '普通旧任务' })).toBe(false)
})
