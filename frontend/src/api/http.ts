import axios, { type AxiosRequestConfig } from 'axios'
import { accessToken, authRole, authNotice, clearSession } from './session'

/** 所有业务客户端共用认证与拒绝处理，保留原有超时和v1响应形状。 */
export function createHttp(config: AxiosRequestConfig) {
  const client = axios.create(config)
  client.interceptors.request.use(request => {
    if (accessToken.value) request.headers.set('Authorization', `Bearer ${accessToken.value}`)
    return request
  })
  client.interceptors.response.use(response => response, error => {
    if (error.response?.status === 401) clearSession('令牌无效或已失效，请重新认证')
    if (error.response?.status === 403 && (authRole.value === 'readonly' || error.response?.data?.error?.code === 'READ_ONLY')) authNotice.value = '当前为只读访问，无权执行此操作'
    return Promise.reject(error)
  })
  return client
}
