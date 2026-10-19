<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { MagicStick, Delete } from '@element-plus/icons-vue'
import { sendChat, type ToolCallRecord } from '../api/chat'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { fetchSkills, runSkill, type SkillInfo, type SkillRunOutput } from '../api/skills'

/** 对话消息：用户/助手文本、工具调用卡片 或 技能执行卡片 */
interface ChatMsg {
  role: 'user' | 'assistant'
  content: string
}
interface ToolMsg {
  role: 'tool'
  tool: ToolCallRecord
}
interface SkillMsg {
  role: 'skill'
  skill: {
    name: string
    query: string
    output: SkillRunOutput
  }
}
type Msg = ChatMsg | ToolMsg | SkillMsg

/** 斜杠命令：/技能名 内容（类 Claude Code） */
const SKILL_CMD_RE = /^\/([a-zA-Z0-9_-]{1,64})(?:\s+([\s\S]*))?$/

/** localStorage 持久化 key（方案 1：前端本地持久化，刷新/重启浏览器不丢） */
const STORAGE_KEY = 'smartbrief-chat'

/** 从 localStorage 恢复历史会话（不存在或数据损坏时返回空） */
function loadStorage(): { messages: Msg[]; history: ChatMsg[] } | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (Array.isArray(parsed.messages) && Array.isArray(parsed.history)) {
      return { messages: parsed.messages, history: parsed.history }
    }
    return null
  } catch {
    return null // 解析失败视为无历史，避免页面崩溃
  }
}

// 初始化：优先恢复本地会话，否则从空开始
const saved = loadStorage()
const messages = ref<Msg[]>(saved?.messages ?? [])
const history = ref<ChatMsg[]>(saved?.history ?? [])
const input = ref('')
const loading = ref(false)

/** 全部技能（含禁用，用于命令执行时的状态判断） */
const allSkills = ref<SkillInfo[]>([])
/** 已启用的技能（供 / 下拉与 + 按钮选择） */
const skillOptions = computed(() => allSkills.value.filter((s) => s.enabled))
/** 输入 / 时展示的技能建议列表 */
const suggestList = ref<SkillInfo[]>([])
const showSuggest = ref(false)
const inputEl = ref<InstanceType<typeof import('element-plus')['ElInput']>>()
const bodyEl = ref<HTMLElement>()

// 启动时加载技能列表（失败静默，仅 / 与 + 入口不可用）
onMounted(() => {
  fetchSkills()
    .then((list) => {
      allSkills.value = list
    })
    .catch(() => {})
  // 恢复历史后滚动到底部
  scrollToBottom()
})

// 消息变化时自动滚动到最新回复
watch(messages, scrollToBottom, { deep: true })

function scrollToBottom() {
  nextTick(() => {
    if (bodyEl.value) {
      bodyEl.value.scrollTop = bodyEl.value.scrollHeight
    }
  })
}

/** 清空当前对话（消息区 + 会话历史；localStorage 由下方 watch 自动同步清除） */
function clearChat() {
  if (messages.value.length === 0) return
  messages.value = []
  history.value = []
}

// 监听消息/历史变化，实时写回 localStorage（deep 监听嵌套对象）
watch(
  [messages, history],
  () => {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ messages: messages.value, history: history.value })
      )
    } catch {
      // 存储不可用（隐私模式/配额超限）时静默失败，不影响对话
    }
  },
  { deep: true }
)

async function handleSend() {
  const text = input.value.trim()
  if (!text || loading.value) return
  input.value = ''
  messages.value.push({ role: 'user', content: text })
  history.value.push({ role: 'user', content: text })

  // 斜杠命令：/技能名 内容 → 直连执行技能
  const m = text.match(SKILL_CMD_RE)
  if (m && m[1]) {
    await handleSkillSend(m[1], m[2]?.trim() ?? '')
    return
  }

  loading.value = true
  try {
    const res = await sendChat(text, history.value)
    // 展示本次工具调用记录（卡片形式穿插在消息流中）
    for (const tc of res.tool_calls) {
      messages.value.push({ role: 'tool', tool: tc })
    }
    messages.value.push({ role: 'assistant', content: res.reply })
    history.value.push({ role: 'assistant', content: res.reply })
  } catch (e) {
    ElMessage.error('对话失败：请检查后端服务与 .env 配置')
    messages.value.push({
      role: 'assistant',
      content: '（工具调用失败，请查看后端日志）'
    })
  } finally {
    loading.value = false
  }
}

/** 直连执行技能：展示技能卡片 + 追加结果上下文 + LLM 自然语言总结 */
async function handleSkillSend(name: string, query: string) {
  const skill = allSkills.value.find((s) => s.name === name)
  if (!skill) {
    ElMessage.error(`未知技能：${name}，输入 / 可查看可用技能`)
    return
  }
  if (!skill.enabled) {
    ElMessage.warning(`技能「${skill.display_name}」已禁用，可在技能中心开启`)
    return
  }
  if (!query) {
    ElMessage.warning('请在技能名后输入内容，例如：/humanizer 把这段话改得更自然')
    return
  }

  loading.value = true
  try {
    const output = await runSkill(name, { query })
    messages.value.push({ role: 'skill', skill: { name, query, output } })

    // 将技能结果作为上下文追加到历史，再请 LLM 基于结果生成自然语言回复
    history.value.push({
      role: 'assistant',
      content: `技能「${skill.display_name}」执行结果：\n${formatJson(output)}`
    })
    const res = await sendChat('请基于上面技能执行的结果，用自然语言回应用户。', history.value)
    for (const tc of res.tool_calls) {
      messages.value.push({ role: 'tool', tool: tc })
    }
    messages.value.push({ role: 'assistant', content: res.reply })
    history.value.push({ role: 'assistant', content: res.reply })
  } catch (e) {
    ElMessage.error('技能执行失败：请检查后端服务与技能配置')
    messages.value.push({
      role: 'assistant',
      content: '（技能执行失败，请查看后端日志）'
    })
  } finally {
    loading.value = false
  }
}

/** Markdown → 安全 HTML（DOMPurify 过滤，防止 XSS） */
function renderMarkdown(md: string): string {
  if (!md) return ''
  const raw = marked.parse(md, { async: false }) as string
  return DOMPurify.sanitize(raw)
}

function formatJson(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

/** 技能展示名（找不到时回退到技能标识） */
function skillDisplayName(name: string): string {
  return allSkills.value.find((s) => s.name === name)?.display_name ?? name
}

/** 输入 / 时更新建议列表（已输入完整命令词后隐藏） */
function updateSuggest(val: string) {
  if (!val.startsWith('/')) {
    showSuggest.value = false
    return
  }
  const q = val.slice(1)
  const hit = skillOptions.value.filter(
    (s) => s.name.includes(q) || s.display_name.includes(q)
  )
  showSuggest.value = hit.length > 0 && !/^\/[\w-]+\s/.test(val)
  suggestList.value = hit
}

function pickSkill(name: string) {
  input.value = `/${name} `
  showSuggest.value = false
  nextTick(() => inputEl.value?.focus())
}
</script>

<template>
  <div class="chat-page">
    <div class="chat-header">
      <h2>AI 对话</h2>
      <span class="hint">试试："现在几点了" / "计算 12*34+56" / "搜索 便携储能 2026 市场"</span>
      <el-button
        class="clear-btn"
        text
        :icon="Delete"
        :disabled="messages.length === 0"
        @click="clearChat"
      >清空</el-button>
    </div>

    <div ref="bodyEl" class="chat-body">
      <el-empty v-if="messages.length === 0" description="输入问题，单智能体将自主决定是否调用工具" />

      <div v-for="(msg, i) in messages" :key="i" class="msg">
        <!-- 用户 -->
        <div v-if="msg.role === 'user'" class="msg-row user">
          <div class="bubble">{{ msg.content }}</div>
        </div>

        <!-- 助手 -->
        <div v-else-if="msg.role === 'assistant'" class="msg-row assistant">
          <div class="bubble bubble-ai">
            <div class="markdown-body" v-html="renderMarkdown(msg.content)"></div>
          </div>
        </div>

        <!-- 工具调用卡片 -->
        <div v-else-if="msg.role === 'tool'" class="msg-row tool">
          <el-card shadow="never" class="tool-card">
            <template #header>
              <div class="tool-card-header">
                <span class="tool-name">{{ msg.tool.name }}</span>
                <el-tag :type="msg.tool.success ? 'success' : 'danger'" size="small">
                  {{ msg.tool.success ? '调用成功' : '调用失败' }}
                </el-tag>
              </div>
            </template>
            <div class="tool-section">
              <div class="tool-label">参数</div>
              <pre>{{ formatJson(msg.tool.arguments) }}</pre>
            </div>
            <div class="tool-section">
              <div class="tool-label">结果</div>
              <pre>{{ formatJson(msg.tool.result) }}</pre>
            </div>
          </el-card>
        </div>

        <!-- 技能执行卡片：结果 JSON 默认收起，点击展开 -->
        <div v-else-if="msg.role === 'skill'" class="msg-row skill">
          <el-card shadow="never" class="skill-card">
            <template #header>
              <div class="skill-card-header">
                <span class="skill-name">
                  <el-icon class="skill-icon"><MagicStick /></el-icon>
                  技能 · {{ skillDisplayName(msg.skill.name) }}
                </span>
                <el-tag
                  :type="msg.skill.output.status === 'success' ? 'success' : 'danger'"
                  size="small"
                >
                  {{ msg.skill.output.status === 'success' ? '执行成功' : '执行失败' }}
                </el-tag>
              </div>
            </template>
            <div class="skill-section">
              <div class="skill-label">输入</div>
              <pre>{{ msg.skill.query }}</pre>
            </div>
            <details class="skill-details">
              <summary>执行结果（点击展开）</summary>
              <pre>{{ formatJson(msg.skill.output) }}</pre>
            </details>
          </el-card>
        </div>
      </div>
    </div>

    <div class="chat-input">
      <div class="input-wrap">
        <el-input
          ref="inputEl"
          v-model="input"
          type="textarea"
          :rows="2"
          placeholder="输入问题回车发送；输入 / 可唤起技能列表（如 /humanizer 把这段话改得更自然）"
          @keydown.enter.exact.prevent="handleSend"
          @input="updateSuggest(input)"
        />

        <!-- / 技能建议下拉 -->
        <div v-if="showSuggest" class="suggest-pop">
          <div
            v-for="s in suggestList"
            :key="s.name"
            class="suggest-item"
            @click="pickSkill(s.name)"
          >
            <span class="suggest-name">{{ s.display_name }}</span>
            <span class="suggest-desc">{{ s.description }}</span>
          </div>
        </div>
      </div>
      <el-dropdown trigger="click" @command="pickSkill" class="skill-btn">
        <el-button class="input-btn">+ 技能</el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item
              v-for="s in skillOptions"
              :key="s.name"
              :command="s.name"
            >
              {{ s.display_name }}（{{ s.name }}）
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-button type="primary" class="input-btn" :loading="loading" @click="handleSend">发送</el-button>
    </div>
  </div>
</template>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 140px);
}

.chat-header {
  display: flex;
  align-items: baseline;
  gap: 16px;
  margin-bottom: 12px;
}
.chat-header h2 {
  margin: 0;
  font-size: 20px;
}
.hint {
  color: #909399;
  font-size: 13px;
}

.clear-btn {
  margin-left: auto;
}
.chat-body {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0;
}

.msg {
  margin-bottom: 12px;
}
.msg-row {
  display: flex;
}
.msg-row.user {
  justify-content: flex-end;
}
.msg-row.assistant {
  justify-content: flex-start;
}
.bubble {
  max-width: 70%;
  padding: 10px 14px;
  border-radius: 8px;
  line-height: 1.6;
  white-space: pre-wrap;
}
.msg-row.user .bubble {
  background: #409eff;
  color: #fff;
}
.bubble-ai {
  background: #fff;
  border: 1px solid #e4e7ed;
}
.bubble pre {
  margin: 0;
  font-family: inherit;
  white-space: pre-wrap;
}

/* Markdown 渲染（AI 回复，v-html 内容需 :deep 命中） */
.markdown-body {
  font-size: 14px;
  line-height: 1.7;
  word-break: break-word;
  color: #303133;
}
.markdown-body :deep(p) {
  margin: 6px 0;
}
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4) {
  margin: 12px 0 6px;
  font-weight: 600;
  line-height: 1.4;
}
.markdown-body :deep(h1) {
  font-size: 18px;
}
.markdown-body :deep(h2) {
  font-size: 16px;
}
.markdown-body :deep(h3) {
  font-size: 15px;
}
.markdown-body :deep(h4) {
  font-size: 14px;
}
.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 6px 0;
  padding-left: 22px;
}
.markdown-body :deep(li) {
  margin: 2px 0;
}
.markdown-body :deep(blockquote) {
  margin: 8px 0;
  padding: 4px 12px;
  border-left: 3px solid #e4e7ed;
  color: #909399;
}
.markdown-body :deep(code) {
  background: #f5f7fa;
  border-radius: 3px;
  padding: 1px 5px;
  font-family: Consolas, Monaco, monospace;
  font-size: 13px;
}
.markdown-body :deep(pre) {
  background: #f5f7fa;
  border-radius: 6px;
  padding: 10px 12px;
  overflow-x: auto;
  margin: 8px 0;
}
.markdown-body :deep(pre code) {
  background: transparent;
  padding: 0;
}
.markdown-body :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
  width: 100%;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid #e4e7ed;
  padding: 6px 10px;
  text-align: left;
}
.markdown-body :deep(th) {
  background: #f5f7fa;
  font-weight: 600;
}
.markdown-body :deep(a) {
  color: #409eff;
}
.markdown-body :deep(hr) {
  border: none;
  border-top: 1px solid #e4e7ed;
  margin: 10px 0;
}

.tool-card {
  width: 100%;
  background: #f7f8fa;
}
.tool-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.tool-name {
  font-weight: 600;
  color: #303133;
}
.tool-section {
  margin-bottom: 8px;
}
.tool-label {
  font-size: 12px;
  color: #909399;
  margin-bottom: 4px;
}
.tool-section pre {
  margin: 0;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 8px;
  font-size: 12px;
  max-height: 180px;
  overflow: auto;
  white-space: pre-wrap;
}

/* 技能执行卡片 */
.skill-card {
  width: 100%;
  background: #f0f9eb;
  border: 1px solid #e1f3d8;
}
.skill-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.skill-name {
  font-weight: 600;
  color: #303133;
  display: flex;
  align-items: center;
  gap: 6px;
}
.skill-icon {
  color: #67c23a;
}
.skill-section {
  margin-bottom: 8px;
}
.skill-label {
  font-size: 12px;
  color: #909399;
  margin-bottom: 4px;
}
.skill-section pre {
  margin: 0;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 8px;
  font-size: 12px;
  white-space: pre-wrap;
}
.skill-details {
  margin: 0;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 4px;
}
.skill-details summary {
  cursor: pointer;
  padding: 6px 8px;
  font-size: 12px;
  color: #909399;
  user-select: none;
}
.skill-details summary:hover {
  color: #67c23a;
}
.skill-details pre {
  margin: 0;
  padding: 8px;
  font-size: 12px;
  border-top: 1px dashed #ebeef5;
  max-height: 240px;
  overflow: auto;
  white-space: pre-wrap;
}

.chat-input {
  display: flex;
  gap: 12px;
  align-items: center;
  padding-top: 12px;
  border-top: 1px solid #e4e7ed;
}
.input-wrap {
  position: relative;
  flex: 1;
  display: flex;
  align-items: center;
}
.input-wrap .el-textarea {
  flex: 1;
}
.skill-btn {
  flex-shrink: 0;
}
/* 技能按钮与发送按钮统一尺寸，垂直居中 */
.input-btn {
  width: 80px;
  height: 40px;
  margin: 0;
}

/* / 技能建议下拉 */
.suggest-pop {
  position: absolute;
  left: 0;
  right: 0;
  bottom: calc(100% + 8px);
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
  max-height: 240px;
  overflow-y: auto;
  z-index: 20;
}
.suggest-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 12px;
  cursor: pointer;
}
.suggest-item:hover {
  background: #f0f9eb;
}
.suggest-name {
  font-weight: 600;
  color: #303133;
  font-size: 13px;
}
.suggest-desc {
  color: #909399;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
