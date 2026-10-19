<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules, UploadFile } from 'element-plus'
import {
  fetchSkills,
  createSkill,
  updateSkill,
  deleteSkill,
  toggleSkill,
  importSkillZip,
  runSkill,
  type SkillInfo,
  type SkillCreateForm,
  type SkillRunOutput
} from '../api/skills'

// ---------------- 分类元数据 ----------------

const categoryMeta: Record<string, { label: string; type: 'primary' | 'success' | 'warning' | 'danger' | 'info' }> = {
  search: { label: '搜索', type: 'primary' },
  analysis: { label: '分析', type: 'success' },
  writing: { label: '写作', type: 'warning' },
  review: { label: '审核', type: 'danger' },
  other: { label: '其它', type: 'info' }
};

function categoryOf(category: string) {
  return categoryMeta[category] ?? { label: category, type: 'info' as const }
}

// ---------------- 数据 ----------------

const skills = ref<SkillInfo[]>([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    skills.value = await fetchSkills()
  } catch {
    ElMessage.error('获取技能列表失败，请检查后端服务')
  } finally {
    loading.value = false
  }
}

onMounted(load)

// ---------------- 新增 / 编辑对话框 ----------------

const DEFAULT_SKILL_MD = `## 技能说明

请根据用户的请求执行本技能的任务，并以 Markdown 输出结构化结果。
`

const dialogVisible = ref(false)
const editingName = ref<string | null>(null) // null = 新增
const formRef = ref<FormInstance>()
const form = reactive<SkillCreateForm>({
  name: '',
  display_name: '',
  description: '',
  category: 'analysis',
  version: '1.0.0',
  skill_md: DEFAULT_SKILL_MD,
  enabled: true
})

const formRules: FormRules = {
  name: [
    { required: true, message: '请输入技能标识', trigger: 'blur' },
    {
      pattern: /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/,
      message: '仅支持字母、数字、下划线、中划线（不能以 -/_ 开头）',
      trigger: 'blur'
    }
  ],
  display_name: [{ required: true, message: '请输入展示名', trigger: 'blur' }],
  category: [{ required: true, message: '请选择分类', trigger: 'change' }]
}

function openCreateDialog() {
  editingName.value = null
  Object.assign(form, {
    name: '',
    display_name: '',
    description: '',
    category: 'analysis',
    version: '1.0.0',
    skill_md: DEFAULT_SKILL_MD,
    enabled: true
  })
  dialogVisible.value = true
}

function openEditDialog(skill: SkillInfo) {
  editingName.value = skill.name
  Object.assign(form, {
    name: skill.name,
    display_name: skill.display_name,
    description: skill.description,
    category: skill.category,
    version: skill.version,
    skill_md: skill.skill_md,
    enabled: skill.enabled
  })
  dialogVisible.value = true
}

async function submitForm() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  try {
    if (editingName.value) {
      const { name: _name, ...payload } = form
      await updateSkill(editingName.value, { ...payload })
      ElMessage.success('技能已更新')
    } else {
      await createSkill({ ...form })
      ElMessage.success('技能已创建')
    }
    dialogVisible.value = false
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败，请检查后端服务')
  }
}

// ---------------- 开关 ----------------

async function onToggle(skill: SkillInfo, enabled: boolean) {
  try {
    const updated = await toggleSkill(skill.name, enabled)
    ElMessage.success(`已${enabled ? '启用' : '禁用'}「${skill.display_name}」`)
    skill.enabled = updated.enabled
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '开关切换失败，请检查后端服务')
  }
}

// ---------------- 删除 ----------------

async function onDelete(skill: SkillInfo) {
  try {
    await ElMessageBox.confirm(
      `确定删除技能「${skill.display_name}」（${skill.name}）吗？其目录与 skill.md 将被移除。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
    await deleteSkill(skill.name)
    ElMessage.success(`已删除「${skill.display_name}」`)
    await load()
  } catch (e: any) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

// ---------------- zip 导入 ----------------

const importing = ref(false)

async function onImportFile(uploadFile: UploadFile) {
  // el-upload 的 on-change 回调传的是 UploadFile 包装对象，必须取 .raw
  // 才是真正的 File；直接传对象会被序列化成 "[object Object]" 导致后端 422。
  if (!uploadFile.raw) {
    ElMessage.error('未获取到文件内容，请重新选择 zip 包')
    return
  }
  importing.value = true
  try {
    const skill = await importSkillZip(uploadFile.raw)
    ElMessage.success(`已导入技能「${skill.display_name}」`)
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '导入失败，请检查 zip 包结构')
  } finally {
    importing.value = false
  }
  return false // 阻止 el-upload 自动上传
}

// ---------------- 试运行 ----------------

const runVisible = ref(false)
const runSkillName = ref('')
const runSkillLabel = ref('')
const runQuery = ref('')
const runLoading = ref(false)
const runResult = ref<SkillRunOutput | null>(null)
const runError = ref('')

function openRunDialog(skill: SkillInfo) {
  runSkillName.value = skill.name
  runSkillLabel.value = skill.display_name
  runQuery.value = ''
  runResult.value = null
  runError.value = ''
  runVisible.value = true
}

async function submitRun() {
  if (!runQuery.value.trim()) {
    ElMessage.warning('请输入试运行请求')
    return
  }
  runLoading.value = true
  runResult.value = null
  runError.value = ''
  try {
    runResult.value = await runSkill(runSkillName.value, { query: runQuery.value.trim() })
  } catch (e: any) {
    runError.value = e?.response?.data?.detail || e.message || '试运行失败，请检查后端服务'
  } finally {
    runLoading.value = false
  }
}

const runStatusLabel = computed(() => {
  if (runError.value) return '请求失败'
  if (!runResult.value) return ''
  return runResult.value.status === 'success' ? '成功' : '失败'
})

const runStatusType = computed(() => {
  if (runError.value) return 'danger'
  if (!runResult.value) return 'info'
  return runResult.value.status === 'success' ? 'success' : 'danger'
})

const runMainContent = computed(() => {
  if (runError.value) return runError.value
  if (!runResult.value) return ''
  if (runResult.value.status === 'failed') {
    return (runResult.value.metadata?.error as string) || '执行失败（无错误信息）'
  }
  return displayResult(runResult.value)
})

function displayResult(data: unknown): string {
  if (data === null || data === undefined) return '（无数据）'
  if (typeof data === 'string') return data
  try {
    return JSON.stringify(data, null, 2)
  } catch {
    return String(data)
  }
}
</script>

<template>
  <div>
    <div class="skill-header">
      <div>
        <h2>技能中心</h2>
        <p class="hint">技能（Skill）是比工具更高一层的能力单元；内置技能只读，自定义技能持久化到 skills_pool/ 目录</p>
      </div>
      <div class="header-actions">
        <el-upload
          :show-file-list="false"
          :auto-upload="false"
          accept=".zip"
          :on-change="onImportFile"
          :disabled="importing"
        >
          <el-button :loading="importing">导入 .zip 技能包</el-button>
        </el-upload>
        <el-button :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" @click="openCreateDialog">新增技能</el-button>
      </div>
    </div>

    <div v-loading="loading" class="skill-grid">
      <el-card
        v-for="skill in skills"
        :key="skill.name"
        shadow="hover"
        class="skill-card"
        :class="{ 'card-disabled': !skill.enabled }"
      >
        <template #header>
          <div class="card-header">
            <span class="card-title">{{ skill.display_name }}</span>
            <div class="card-header-right">
              <el-tag
                :type="skill.builtin ? 'info' : 'success'"
                size="small"
              >{{ skill.builtin ? '内置' : '自定义' }}</el-tag>
              <el-switch
                :model-value="skill.enabled"
                @change="(v: string | number | boolean) => onToggle(skill, Boolean(v))"
                inline-prompt
                active-text="开"
                inactive-text="关"
              />
            </div>
          </div>
        </template>
        <div class="card-body">
          <div class="meta-row">
            <el-tag :type="categoryOf(skill.category).type" size="small">
              {{ categoryOf(skill.category).label }}
            </el-tag>
            <span class="name">{{ skill.name }}</span>
            <span class="version">v{{ skill.version }}</span>
          </div>
          <p class="desc">{{ skill.description || '（暂无描述）' }}</p>
        </div>
        <div class="card-footer">
          <el-button
            link
            type="primary"
            size="small"
            :disabled="!skill.enabled"
            @click="openRunDialog(skill)"
          >试运行</el-button>
          <el-button
            link
            type="primary"
            size="small"
            :disabled="skill.builtin"
            @click="openEditDialog(skill)"
          >编辑</el-button>
          <el-button
            link
            type="danger"
            size="small"
            :disabled="skill.builtin"
            @click="onDelete(skill)"
          >删除</el-button>
        </div>
      </el-card>
      <el-empty v-if="skills.length === 0 && !loading" description="暂无技能，可点击「新增技能」创建" />
    </div>

    <!-- ============ 新增 / 编辑对话框 ============ -->
    <el-dialog
      v-model="dialogVisible"
      :title="editingName ? `编辑技能「${editingName}」` : '新增技能'"
      width="620px"
      @closed="formRef?.clearValidate()"
    >
      <el-form ref="formRef" :model="form" :rules="formRules" label-width="90px">
        <el-form-item label="标识" prop="name">
          <el-input
            v-model="form.name"
            :disabled="!!editingName"
            placeholder="例如：weekly_report"
            maxlength="64"
          />
        </el-form-item>
        <el-form-item label="展示名" prop="display_name">
          <el-input v-model="form.display_name" placeholder="例如：周报生成" maxlength="64" />
        </el-form-item>
        <el-form-item label="描述" prop="description">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="2"
            placeholder="该技能做什么（可选）"
            maxlength="500"
          />
        </el-form-item>
        <el-form-item label="分类" prop="category">
          <el-select v-model="form.category" style="width: 200px">
            <el-option label="搜索" value="search" />
            <el-option label="分析" value="analysis" />
            <el-option label="写作" value="writing" />
            <el-option label="审核" value="review" />
            <el-option label="其它" value="other" />
          </el-select>
          <el-input v-model="form.version" placeholder="版本号" style="width: 140px; margin-left: 12px" maxlength="32" />
        </el-form-item>
        <el-form-item label="skill.md">
          <el-input
            v-model="form.skill_md"
            type="textarea"
            :rows="8"
            placeholder="Markdown 指令，说明该技能如何执行（留空使用默认模板）"
          />
          <span class="hint">无 skill.py 时，后端将按此说明调用 LLM 执行</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitForm">保存</el-button>
      </template>
    </el-dialog>

    <!-- ============ 试运行对话框 ============ -->
    <el-dialog v-model="runVisible" :title="`试运行「${runSkillLabel}」`" width="620px">
      <el-input
        v-model="runQuery"
        type="textarea"
        :rows="3"
        placeholder="输入请求内容，例如：帮我搜索 AI 行业最新动态"
      />
      <div class="run-actions">
        <el-button type="primary" :loading="runLoading" @click="submitRun">执行</el-button>
      </div>
      <div v-if="runResult || runError" class="run-result">
        <div class="run-status">
          <el-tag :type="runStatusType" size="small">{{ runStatusLabel }}</el-tag>
          <span v-if="runResult?.metadata && runResult.status === 'success'" class="hint">
            {{ JSON.stringify(runResult.metadata) }}
          </span>
        </div>
        <pre class="run-output">{{ runMainContent }}</pre>
        <details v-if="runResult?.status === 'failed'" class="run-raw">
          <summary>完整响应</summary>
          <pre class="run-output">{{ displayResult(runResult) }}</pre>
        </details>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.skill-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}
.skill-header h2 {
  margin: 0;
  font-size: 20px;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.hint {
  color: #909399;
  font-size: 13px;
}
.skill-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px;
  min-height: 120px;
}
.skill-card .card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
/* 卡片整体定高：标题区 + 元信息 + 描述 + 底部操作 = 220px */
:deep(.el-card__body) {
  display: flex;
  flex-direction: column;
  height: 172px;
  padding: 16px;
  box-sizing: border-box;
}
.skill-card .card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 24px;
  overflow: hidden;
}
.card-header-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.card-disabled {
  opacity: 0.55;
}
.skill-card .card-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding-right: 8px;
}
.skill-card .meta-row {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 28px; /* 固定高度：分类标签 + 标识 + 版本号单行展示 */
  margin-bottom: 8px;
  overflow: hidden;
}
.skill-card .name {
  flex: 1;
  min-width: 0;
  color: #606266;
  font-size: 13px;
  font-family: monospace;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.skill-card .version {
  color: #c0c4cc;
  font-size: 12px;
  white-space: nowrap;
}
.skill-card .desc {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
  overflow: hidden;
  color: #606266;
  font-size: 13px;
  line-height: 20px;
  height: 60px; /* 固定三行高度 */
  margin: 0;
  flex: 1;
}
.card-footer {
  border-top: 1px solid #f0f2f5;
  padding-top: 8px;
  margin-top: auto;
  display: flex;
  justify-content: flex-end;
}
.run-actions {
  margin: 12px 0;
  display: flex;
  justify-content: flex-end;
}
.run-status {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.run-output {
  background: #f5f7fa;
  border-radius: 4px;
  padding: 12px;
  font-size: 13px;
  color: #303133;
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
.run-raw {
  margin-top: 8px;
  font-size: 12px;
  color: #606266;
}
.run-raw summary {
  cursor: pointer;
  user-select: none;
}
.run-raw .run-output {
  margin-top: 8px;
  max-height: 200px;
}
</style>
