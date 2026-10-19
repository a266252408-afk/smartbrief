import axios from 'axios'

/** 技能信息（列表 / 表单回显共用） */
export interface SkillInfo {
  name: string
  display_name: string
  description: string
  /** search | analysis | writing | review */
  category: string
  version: string
  /** 内置技能不可删除 / 修改 */
  builtin: boolean
  /** 开关：false 时不可试运行 */
  enabled: boolean
  /** builtin | custom */
  source: string
  /** skill.md Markdown 正文（不含 frontmatter） */
  skill_md: string
}

/** 新增技能表单 */
export interface SkillCreateForm {
  name: string
  display_name: string
  description: string
  category: string
  version: string
  skill_md: string
  enabled: boolean
}

/** 编辑技能表单（name 不可改） */
export type SkillUpdateForm = Omit<Partial<SkillCreateForm>, 'name'>

/** 试运行请求 */
export interface SkillRunForm {
  query: string
  context?: Record<string, unknown>
}

/** 试运行输出 */
export interface SkillRunOutput {
  status: 'success' | 'failed'
  data: unknown
  metadata?: Record<string, unknown>
}

const http = axios.create({
  baseURL: '/api/v1',
  timeout: 15000
})

/** 获取技能列表 */
export async function fetchSkills(): Promise<SkillInfo[]> {
  const { data } = await http.get('/skills')
  return data.skills as SkillInfo[]
}

/** 新增自定义技能 */
export async function createSkill(form: SkillCreateForm): Promise<SkillInfo> {
  const { data } = await http.post('/skills', form)
  return data.skill as SkillInfo
}

/** 编辑自定义技能 */
export async function updateSkill(name: string, form: SkillUpdateForm): Promise<SkillInfo> {
  const { data } = await http.put(`/skills/${name}`, form)
  return data.skill as SkillInfo
}

/** 删除技能（内置技能会被后端拒绝） */
export async function deleteSkill(name: string): Promise<void> {
  await http.delete(`/skills/${name}`)
}

/** 开关技能（内置 / 自定义均可） */
export async function toggleSkill(name: string, enabled: boolean): Promise<SkillInfo> {
  const { data } = await http.put(`/skills/${name}/enabled`, { enabled })
  return data.skill as SkillInfo
}

/** 导入 .zip 技能包 */
export async function importSkillZip(file: File): Promise<SkillInfo> {
  const fd = new FormData()
  fd.append('file', file)
  // 注意：不要手动设置 Content-Type——axios 对 FormData 会自动生成
  // multipart/form-data; boundary=...；手动设置会丢失 boundary 导致后端 422。
  const { data } = await http.post('/skills/import', fd)
  return data.skill as SkillInfo
}

/** 试运行技能（LLM 技能响应慢，单独放宽到 60s） */
export async function runSkill(name: string, form: SkillRunForm): Promise<SkillRunOutput> {
  const { data } = await http.post(`/skills/${name}/run`, form, { timeout: 60000 })
  return data.output as SkillRunOutput
}
