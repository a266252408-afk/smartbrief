<script setup lang="ts">
import { ref } from 'vue'
import { createHttp } from '../api/http'
import { accessToken, authRole, authNotice, clearSession, type AuthRole } from '../api/session'
const input = ref('')
const busy = ref(false)
const client = createHttp({ baseURL: '/api/v2', timeout: 15000 })
async function authenticate() {
  if (busy.value) return
  if (!input.value.trim()) { authNotice.value = '请输入访问令牌'; return }
  busy.value = true
  accessToken.value = input.value.trim()
  input.value = ''
  authNotice.value = ''
  try {
    const { data } = await client.get<{ data: { role: AuthRole }; meta: { request_id: string } }>('/auth/session')
    if (!['admin', 'readonly'].includes(data.data.role)) throw new Error('无效角色')
    authRole.value = data.data.role
  } catch {
    clearSession('认证失败，请检查访问令牌或服务端配置')
  } finally { busy.value = false }
}
</script>
<template>
  <section class="auth-panel">
    <h2>访问认证</h2>
    <p>请输入管理员或只读访问令牌。凭证仅保存在本页面内存，刷新后需重新输入。</p>
    <el-input v-model="input" type="password" placeholder="访问令牌" autocomplete="off"
      aria-label="访问令牌" @keydown.enter.prevent="authenticate" />
    <el-button type="primary" :loading="busy" @click="authenticate">验证并进入</el-button>
    <p v-if="authNotice" role="alert">{{ authNotice }}</p>
  </section>
</template>
<style scoped>
.auth-panel { max-width: 480px; margin: 80px auto; padding: 32px; background: white; }
.auth-panel p { line-height: 1.7; }
.auth-panel .el-button { margin-top: 16px; }
</style>
