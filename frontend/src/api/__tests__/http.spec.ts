import { afterEach, describe, expect, it } from 'vitest'
import { AxiosError, AxiosHeaders, type AxiosResponse } from 'axios'
import { createHttp } from '../http'
import { accessToken, authRole, authNotice, clearSession } from '../session'

afterEach(() => clearSession())
describe('统一认证客户端', () => {
  it('通过头传递令牌，不放入URL', async () => {
    accessToken.value = 'fixture-token'
    const client = createHttp({ adapter: async config => {
      expect(config.headers.get('Authorization')).toBe('Bearer fixture-token')
      expect(config.url).toBe('/tasks')
      expect(config.params).toBeUndefined()
      return { data: {}, status: 200, statusText: 'OK', headers: {}, config }
    } })
    await client.get('/tasks')
  })
  for (const status of [401,403]) {
    it(`${status}按语义清理或保留会话`, async () => {
      accessToken.value = 'fixture-token'
      authRole.value = 'readonly'
      const client = createHttp({ adapter: async config => {
        const response: AxiosResponse = { data:{}, status, statusText:'Denied', headers:new AxiosHeaders(), config }
        throw new AxiosError('拒绝',undefined,config,undefined,response)
      } })
      await expect(client.get('/tasks')).rejects.toThrow('拒绝')
      expect(Boolean(accessToken.value)).toBe(status === 403)
      expect(authRole.value).toBe(status === 403 ? 'readonly' : null)
      expect(authNotice.value).toBeTruthy()
    })
  }
})

it('真实评测预算拒绝不会把管理员误提示为只读', async () => {
  authRole.value = 'admin'; accessToken.value = 'fixture-token'
  const client = createHttp({ adapter: async config => {
    throw new AxiosError('预算不足', undefined, config, undefined, { data: { error: { code: 'JUDGE_NOT_AUTHORIZED', message: '预算不足' } }, status: 403, statusText: 'Denied', headers: new AxiosHeaders(), config })
  } })
  await expect(client.post('/judge')).rejects.toThrow('预算不足')
  expect(authRole.value).toBe('admin')
  expect(authNotice.value).toBe('')
})
