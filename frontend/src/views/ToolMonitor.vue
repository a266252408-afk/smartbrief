<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import {
  fetchBuiltinTools,
  toggleBuiltinTool,
  fetchMCPServers,
  addMCPServer,
  updateMCPServer,
  deleteMCPServer,
  toggleMCPServer,
  refreshMCPServer,
  fetchToolStats,
  type BuiltinToolInfo,
  type MCPServerInfo,
  type MCPServerForm,
  type ToolStat
} from '../api/tools'

const activeTab = ref<'builtin' | 'mcp'>('builtin')

const builtinTools = ref<BuiltinToolInfo[]>([])
const mcpServers = ref<MCPServerInfo[]>([])
const stats = ref<Record<string, ToolStat>>({})
const loading = ref(false)

// ---------------- 对话框（MCP Server 添加/编辑） ----------------
const dialogVisible = ref(false)
const editingId = ref<string | null>(null) // null = 新增
const formRef = ref<FormInstance>()
const form = reactive<MCPServerForm>({
  name: '',
  url: '',
  description: '',
  enabled: true,
  transport: 'http'
})

const formRules: FormRules = {
  name: [{ required: true, message: '请输入 Server 名称', trigger: 'blur' }],
  url: [
    { required: true, message: '请输入 MCP Server URL', trigger: 'blur' },
    { pattern: /^https?:\/\/.+/, message: 'URL 需以 http:// 或 https:// 开头', trigger: 'blur' }
  ]
}

// ---------------- 数据加载 ----------------

async function load() {
  loading.value = true
  try {
    const [b, m, s] = await Promise.all([
      fetchBuiltinTools(),
      fetchMCPServers(),
      fetchToolStats()
    ])
    builtinTools.value = b
    mcpServers.value = m
    stats.value = s
  } catch (e) {
    ElMessage.error('获取工具列表失败，请检查后端服务')
  } finally {
    loading.value = false
  }
}

onMounted(load)

// ---------------- 内置工具操作 ----------------

async function onToggleBuiltin(tool: BuiltinToolInfo) {
  try {
    await toggleBuiltinTool(tool.name, tool.enabled)
    ElMessage.success(`内置工具「${tool.name}」已${tool.enabled ? '开启' : '关闭'}`)
  } catch (e) {
    tool.enabled = !tool.enabled // 失败回滚
    ElMessage.error('开关失败：请检查后端服务')
  }
}

// ---------------- MCP Server 操作 ----------------

function openAddDialog() {
  editingId.value = null
  Object.assign(form, {
    name: '',
    url: '',
    description: '',
    enabled: true,
    transport: 'http'
  })
  dialogVisible.value = true
}

function openEditDialog(server: MCPServerInfo) {
  editingId.value = server.id
  Object.assign(form, {
    name: server.name,
    url: server.url,
    description: server.description,
    enabled: server.enabled,
    transport: server.kind === 'sse' ? 'sse' : 'http'
  })
  dialogVisible.value = true
}

async function submitForm() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  try {
    const server = editingId.value
      ? await updateMCPServer(editingId.value, { ...form })
      : await addMCPServer({ ...form })
    // 连接失败时后端仍然保存了配置：提示「已保存但连接失败」而不是报错，
    // 避免出现「前端报错、实际已写入」的状态割裂
    if (server.last_error) {
      ElMessage.warning(`已保存，但连接失败：${server.last_error}`)
    } else {
      ElMessage.success(editingId.value ? 'MCP Server 已更新' : 'MCP Server 已添加')
    }
    dialogVisible.value = false
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败，请检查 URL 与后端服务')
  }
}

async function onDelete(server: MCPServerInfo) {
  try {
    await ElMessageBox.confirm(
      `确定删除 MCP Server「${server.name}」吗？该操作不可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
    await deleteMCPServer(server.id)
    ElMessage.success(`已删除「${server.name}」`)
    await load()
  } catch (e: any) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

async function onToggleMCPServer(server: MCPServerInfo) {
  try {
    await toggleMCPServer(server.id, server.enabled)
    ElMessage.success(`「${server.name}」已${server.enabled ? '开启' : '关闭'}`)
  } catch (e) {
    server.enabled = !server.enabled // 失败回滚
    ElMessage.error('开关失败：请检查后端服务')
  }
}

async function onRefresh(server: MCPServerInfo) {
  try {
    const tools = await refreshMCPServer(server.id)
    ElMessage.success(`已刷新，发现 ${tools.length} 个工具`)
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '刷新失败')
  }
}
</script>

<template>
  <div>
    <div class="tool-header">
      <h2>工具管理</h2>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-tabs v-model="activeTab">
      <!-- ============ 内置工具 ============ -->
      <el-tab-pane label="内置工具（Function Calling）" name="builtin">
        <el-card shadow="never">
          <el-table :data="builtinTools" v-loading="loading" stripe>
            <el-table-column prop="name" label="工具名" width="180" />
            <el-table-column label="分类" width="140">
              <template #default="{ row }">
                <el-tag size="small">{{ row.metadata?.category || 'system' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="description" label="描述" min-width="220" />
            <el-table-column label="调用次数" width="110">
              <template #default="{ row }">
                {{ stats[row.name]?.count ?? 0 }}
              </template>
            </el-table-column>
            <el-table-column label="成功 / 失败" width="130">
              <template #default="{ row }">
                <span class="ok">{{ stats[row.name]?.success ?? 0 }}</span>
                <span class="sep">/</span>
                <span class="fail">{{ stats[row.name]?.failed ?? 0 }}</span>
              </template>
            </el-table-column>
            <el-table-column label="开关" width="100" align="center">
              <template #default="{ row }">
                <el-switch
                  v-model="row.enabled"
                  :loading="loading"
                  @change="onToggleBuiltin(row)"
                />
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="builtinTools.length === 0" description="暂无内置工具" />
        </el-card>
      </el-tab-pane>

      <!-- ============ MCP 工具 ============ -->
      <el-tab-pane label="MCP 工具" name="mcp">
        <div class="mcp-toolbar">
          <span class="hint">MCP Server 通过 JSON-RPC over HTTP 提供工具，可自定义添加 / 编辑 / 删除</span>
          <el-button type="primary" @click="openAddDialog">添加 MCP Server</el-button>
        </div>

        <el-card shadow="never">
          <el-table :data="mcpServers" v-loading="loading" stripe :row-style="{ height: '58px' }">
            <el-table-column prop="name" label="名称" width="170">
              <template #default="{ row }">
                {{ row.name }}
                <el-tag v-if="row.builtin" type="info" size="small">内置</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="连接方式" width="130">
              <template #default="{ row }">
                <el-tag
                  v-if="row.kind === 'stdio'"
                  type="warning"
                  size="small"
                >stdio 子进程</el-tag>
                <el-tag
                  v-else-if="row.kind === 'sse'"
                  type="info"
                  size="small"
                >HTTP + SSE</el-tag>
                <el-tag v-else type="success" size="small">Streamable HTTP</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="url" label="URL" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">{{ row.url || '—（本地子进程）' }}</template>
            </el-table-column>
            <el-table-column label="工具" width="260">
              <template #default="{ row }">
                <el-scrollbar
                  v-if="row.tools.length > 0"
                  class="tools-scroll"
                  wrap-class="tools-wrap"
                  view-class="tools-view"
                >
                  <div class="tools-list">
                    <el-tag
                      v-for="t in row.tools"
                      :key="t.name"
                      size="small"
                      class="tool-tag"
                      :title="t.description || t.name"
                    >{{ t.name }}</el-tag>
                  </div>
                </el-scrollbar>
                <span v-else class="no-tools">无工具（可刷新重试）</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="100" align="center">
              <template #default="{ row }">
                <el-switch
                  v-model="row.enabled"
                  @change="onToggleMCPServer(row)"
                />
              </template>
            </el-table-column>
            <el-table-column label="操作" width="230" align="center">
              <template #default="{ row }">
                <el-button
                  link
                  type="primary"
                  size="small"
                  @click="onRefresh(row)"
                >刷新</el-button>
                <el-button
                  link
                  type="primary"
                  size="small"
                  :disabled="row.builtin"
                  @click="openEditDialog(row)"
                >编辑</el-button>
                <el-button
                  link
                  type="danger"
                  size="small"
                  :disabled="row.builtin"
                  @click="onDelete(row)"
                >删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="mcpServers.length === 0" description="暂无 MCP Server" />
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- ============ 添加 / 编辑对话框 ============ -->
    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑 MCP Server' : '添加 MCP Server'"
      width="520px"
      @closed="formRef?.clearValidate()"
    >
      <el-form ref="formRef" :model="form" :rules="formRules" label-width="90px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="例如：my_search_server" maxlength="64" />
        </el-form-item>
        <el-form-item label="传输方式" prop="transport">
          <el-select v-model="form.transport" style="width: 100%">
            <el-option label="Streamable HTTP（推荐）" value="http" />
            <el-option label="HTTP + SSE（旧版）" value="sse" />
          </el-select>
        </el-form-item>
        <el-form-item label="URL" prop="url">
          <el-input
            v-model="form.url"
            :placeholder="form.transport === 'sse' ? '例如：http://localhost:9000/sse' : '例如：http://localhost:9000/mcp'"
          />
        </el-form-item>
        <el-form-item label="描述" prop="description">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="2"
            placeholder="该 Server 提供哪些工具（可选）"
            maxlength="200"
          />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
          <span class="hint" style="margin-left: 8px">
            {{ form.enabled ? '保存后立即发现工具' : '保存后处于关闭状态' }}
          </span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitForm">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.tool-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.tool-header h2 {
  margin: 0;
  font-size: 20px;
}
.mcp-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.hint {
  color: #909399;
  font-size: 13px;
}
.tool-tag {
  margin: 2px 4px 2px 0;
  flex-shrink: 0;
  cursor: default;
}
.tools-scroll {
  height: 100%;
}
.tools-scroll :deep(.tools-wrap) {
  height: 100%;
}
.tools-scroll :deep(.tools-view) {
  max-height: 52px;
  line-height: 1;
}
.tools-list {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  padding: 4px 0;
  gap: 2px 0;
}
.no-tools {
  color: #c0c4cc;
  font-size: 12px;
}
.ok {
  color: #67c23a;
  font-weight: 600;
}
.fail {
  color: #f56c6c;
  font-weight: 600;
}
.sep {
  margin: 0 6px;
  color: #c0c4cc;
}
</style>
