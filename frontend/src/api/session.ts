import { ref } from 'vue'

export type AuthRole = 'admin' | 'readonly'
// 凭证只存在页面内存；不写localStorage、sessionStorage、URL或日志。
export const accessToken = ref('')
export const authRole = ref<AuthRole | null>(null)
export const authNotice = ref('')
export function clearSession(message = '') {
  accessToken.value = ''
  authRole.value = null
  authNotice.value = message
}
