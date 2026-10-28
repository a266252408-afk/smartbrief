import type { TaskMetadata } from '../api/tasks'

/** 明确模式优先；兼容只有系统主题前缀的历史离线任务。 */
export function isOfflineTask(task: { metadata?: TaskMetadata; topic: string }): boolean {
  const mode = task.metadata?.execution_mode
  return mode === 'offline' || (mode == null && task.topic.startsWith('[离线协议] '))
}
