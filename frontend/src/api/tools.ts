import axios from 'axios'

/** 内置工具信息 */
export interface BuiltinToolInfo {
  name: string
  type: string
  description: string
  enabled: boolean
  metadata?: Record<string, string>
}

/** MCP 工具信息（某 Server 下的具体工具） */
export interface MCPToolInfo {
  name: string
  description: string
  schema?: unknown
}

/** MCP Server 信息 */
export interface MCPServerInfo {
  id: string
  name: string
  /** 传输方式：stdio 内置子进程 | http Streamable HTTP | sse HTTP+SSE */
  kind: string
  url: string
  description: string
  enabled: boolean
  /** 内置 Server 不可删除/编辑 */
  builtin: boolean
  tools: MCPToolInfo[]
  last_error?: string
}

/** MCP 远程传输方式 */
export type MCPTransport = 'http' | 'sse'

/** MCP Server 表单（添加/编辑） */
export interface MCPServerForm {
  name: string
  url: string
  description: string
  enabled: boolean
  /** 传输方式：http（Streamable HTTP）| sse（HTTP+SSE） */
  transport: MCPTransport
}

/** 工具调用统计 */
export interface ToolStat {
  count: number
  success: number
  failed: number
}

const http = axios.create({
  baseURL: '/api/v1',
  timeout: 10000
})

// ---------------------------------------------------------------- 内置工具

/** 获取内置工具列表（含开关状态） */
export async function fetchBuiltinTools(): Promise<BuiltinToolInfo[]> {
  const { data } = await http.get('/tools/builtin')
  return data.tools as BuiltinToolInfo[]
}

/** 开关内置工具 */
export async function toggleBuiltinTool(name: string, enabled: boolean): Promise<void> {
  await http.put(`/tools/builtin/${name}`, { enabled })
}

// ---------------------------------------------------------------- MCP 工具

/** 获取 MCP Server 列表（含工具、开关状态） */
export async function fetchMCPServers(): Promise<MCPServerInfo[]> {
  const { data } = await http.get('/tools/mcp')
  return data.servers as MCPServerInfo[]
}

/** 添加自定义 MCP Server */
export async function addMCPServer(form: MCPServerForm): Promise<MCPServerInfo> {
  const { data } = await http.post('/tools/mcp', form)
  return data.server as MCPServerInfo
}

/** 编辑自定义 MCP Server */
export async function updateMCPServer(id: string, form: MCPServerForm): Promise<MCPServerInfo> {
  const { data } = await http.put(`/tools/mcp/${id}`, form)
  return data.server as MCPServerInfo
}

/** 删除自定义 MCP Server */
export async function deleteMCPServer(id: string): Promise<void> {
  await http.delete(`/tools/mcp/${id}`)
}

/** 开关 MCP Server */
export async function toggleMCPServer(id: string, enabled: boolean): Promise<MCPServerInfo> {
  const { data } = await http.put(`/tools/mcp/${id}/enabled`, { enabled })
  return data.server as MCPServerInfo
}

/** 手动刷新 Server 的工具列表 */
export async function refreshMCPServer(id: string): Promise<MCPToolInfo[]> {
  const { data } = await http.post(`/tools/mcp/${id}/refresh`)
  return data.tools as MCPToolInfo[]
}

// ---------------------------------------------------------------- 统计

/** 获取工具调用统计 */
export async function fetchToolStats(): Promise<Record<string, ToolStat>> {
  const { data } = await http.get('/tools/stats')
  return data.stats as Record<string, ToolStat>
}
