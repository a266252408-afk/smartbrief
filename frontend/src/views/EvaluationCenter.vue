<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { fetchCatalog, fetchCatalogCase, startMechanism, fetchMechanismRuns, fetchMechanismRun, type MechanismRun, type EvaluationCatalog, type CatalogDetail, type CatalogCase } from '../api/evaluationCenter'
import { fetchEvalReports, runEvaluation, type EvalReportSummary } from '../api/evaluation'
import { authRole } from '../api/session'

const route = useRoute(), router = useRouter()
const catalog = ref<EvaluationCatalog | null>(null), error = ref(''), loading = ref(false)
const selected = ref<CatalogDetail | null>(null), detailError = ref(''), detailLoading = ref(false)
const area = ref(''), collection = ref(''), query = ref('')
const history = ref<EvalReportSummary[]>([]), historyError = ref(''), starting = ref(false)
const flags = ref({ rag: true, memory: true, review: true })
const mechanismCases = ref(['R009', 'G023']), mechanismRuns = ref<MechanismRun[]>([]), mechanismError = ref('')
const activeRun = ref<MechanismRun | null>(null)
let runPoll: ReturnType<typeof setTimeout> | undefined
let disposed = false
const tab = computed(() => String(route.meta.evaluationTab || 'overview'))
const tabs = [ ['overview', '总览', '/evaluation'], ['datasets', '测试集与标准', '/evaluation/datasets'],
  ['run', '运行评测', '/evaluation/run'], ['records', '评测记录', '/evaluation/records'],
  ['review', '人工复核', '/evaluation/review'], ['compare', '对照实验', '/evaluation/compare'] ]
const collectionLabels: Record<string, string> = { golden: '黄金集', regression: '回归集', challenge: '挑战集', legacy: '原协议集' }
const splitLabels: Record<string, string> = { development: '开发组', holdout: '留出组', evaluation_only: '仅验收', legacy: '原协议' }
const runLabels: Record<string, string> = { running: '执行中', completed: '已处理完', failed: '执行中断' }
const checkLabels: Record<string, string> = { pass: '通过', fail: '未通过', unknown: '待确认' }
function criterion(text: string) { return text.replace('oracle.required_fact_ids', '参考事实清单') }
const rows = computed(() => (catalog.value?.cases || []).filter(c => (!area.value || c.area === area.value)
  && (!collection.value || c.collection === collection.value) && (!query.value || `${c.case_id} ${c.title}`.includes(query.value))))
let detailVersion = 0
function onRow(row: CatalogCase) { void inspect(row.case_id) }
async function load() {
  loading.value = true; error.value = ''
  try { catalog.value = await fetchCatalog() }
  catch { catalog.value = null; error.value = '题库读取失败，请检查数据完整性或重试' }
  finally { loading.value = false }
}
async function loadHistory() {
  historyError.value = ''
  try { history.value = (await fetchEvalReports()).items }
  catch { history.value = []; historyError.value = '评测记录读取失败，请重试' }
}
async function loadMechanisms() {
  mechanismError.value = ''
  try { mechanismRuns.value = await fetchMechanismRuns() }
  catch { mechanismError.value = '专项运行记录不可读，请重试' }
}
async function loadRun() {
  const id = String(route.params.runId || '')
  if (!id || disposed) return
  mechanismError.value = ''
  try {
    const data = await fetchMechanismRun(id)
    if (disposed || id !== String(route.params.runId || '')) return
    activeRun.value = data
    if (data.execution_status === 'running') runPoll = setTimeout(loadRun, 2000)
  } catch { if (!disposed) mechanismError.value = '专项详情不可读，请重试' }
}
async function startRobustness() {
  if (starting.value || authRole.value !== 'admin') return
  starting.value = true; mechanismError.value = ''
  try { const data = await startMechanism(mechanismCases.value); await router.push(`/evaluation/runs/${encodeURIComponent(data.run_id)}`) }
  catch { mechanismError.value = '专项未启动，可能有运行中的评测或案例尚未接通' }
  finally { starting.value = false }
}
async function inspect(id: string) {
  const version = ++detailVersion
  selected.value = null; detailError.value = ''; detailLoading.value = true
  try { const detail = await fetchCatalogCase(id); if (version === detailVersion) selected.value = detail }
  catch { if (version === detailVersion) detailError.value = '案例详情读取失败，请重试' }
  finally { if (version === detailVersion) detailLoading.value = false }
}
async function startProtocol() {
  if (starting.value || authRole.value !== 'admin') return
  starting.value = true; error.value = ''
  try { const result = await runEvaluation(flags.value); await router.push(`/evaluation/protocol?eval=${encodeURIComponent(result.eval_id)}`) }
  catch { error.value = '评测未启动，请检查权限或是否已有运行中的评测' }
  finally { starting.value = false }
}
function loadTab() {
  clearTimeout(runPoll)
  if (['overview', 'records', 'compare'].includes(tab.value)) void loadHistory()
  if (tab.value === 'records') void loadMechanisms()
  if (tab.value === 'run-detail') void loadRun()
}
watch(() => route.fullPath, loadTab)
onMounted(() => { void load(); loadTab() })
onBeforeUnmount(() => { disposed = true; clearTimeout(runPoll) })
</script>

<template>
  <section class="evaluation-center" data-testid="evaluation-center">
    <header><h2>评测中心</h2><p>按能力查看题目、运行过程与证据；不同类型的成绩分别展示。</p></header>
    <nav aria-label="评测中心分区">
      <router-link v-for="t in tabs" :key="t[0]" :to="t[2]!" :class="{ current: tab === t[0] }">{{ t[1] }}</router-link>
    </nav>
    <p v-if="error" role="alert">{{ error }} <el-button @click="load">重新读取题库</el-button></p>
    <p v-if="loading">正在读取版本化题库…</p>
    <p v-if="mechanismError" role="alert">{{ mechanismError }}</p>
    <template v-if="catalog">
      <div class="version">{{ catalog.version }} · 截止 {{ catalog.cutoff }} · {{ catalog.total_cases }} 例 · 题目准备状态不代表系统成绩</div>
      <template v-if="tab === 'overview'">
        <div class="areas">
          <article v-for="a in catalog.areas" :key="a.area">
            <h3>{{ a.label }}</h3><p>{{ a.case_count }} 例</p>
            <strong>{{ a.score === null ? '尚无本板块实测成绩' : a.score }}</strong>
            <p>{{ a.availability }}</p>
            <el-button @click="area = a.area; router.push('/evaluation/datasets')">查看测试题</el-button>
          </article>
        </div>
        <p>原协议历史：{{ history.length }} 条（当前分页）；合成协议结果不能作为真实研究质量成绩。</p>
      </template>
      <template v-else-if="tab === 'datasets'">
        <h3>测试集与标准</h3>
        <p>黄金表示有参考依据；人工抽查仍为待确认。点击案例查看来源、标准和合成条件。</p>
        <div class="filters">
          <el-select v-model="collection" clearable placeholder="全部集合" aria-label="筛选集合"><el-option v-for="(label, key) in collectionLabels" :key="key" :value="key" :label="label" /></el-select>
          <el-select v-model="area" clearable placeholder="全部板块" aria-label="筛选板块"><el-option v-for="a in catalog.areas" :key="a.area" :value="a.area" :label="a.label" /></el-select>
          <el-input v-model="query" placeholder="搜索案例编号或题目" aria-label="搜索案例" />
        </div>
        <p>筛选结果 {{ rows.length }} 例</p>
        <el-table :data="rows" @row-click="onRow" max-height="430">
          <el-table-column prop="case_id" label="编号" width="95" />
          <el-table-column prop="title" label="题目" min-width="260" />
          <el-table-column label="集合" width="100"><template #default="{ row }">{{ collectionLabels[row.collection] }}</template></el-table-column>
          <el-table-column label="资料分组" width="140"><template #default="{ row }">{{ splitLabels[row.split] || '其他分组' }}</template></el-table-column>
          <el-table-column label="人工核验" width="110"><template #default>待抽查</template></el-table-column>
          <el-table-column label="操作" width="95"><template #default="{ row }"><el-button link @click.stop="inspect(row.case_id)">查看详情</el-button></template></el-table-column>
        </el-table>
        <p v-if="detailLoading">正在读取案例…</p><p v-if="detailError" role="alert">{{ detailError }}</p>
        <article v-if="selected" class="case-detail" data-testid="catalog-detail">
          <h3>{{ selected.case.case_id }} · {{ selected.case.title }}</h3>
          <p>{{ selected.case.input.user_request }}</p>
          <p v-if="selected.case.oracle">验收要求如下；未实际运行前不生成通过成绩。</p>
          <ul><li v-for="a in selected.case.oracle?.assertions || []" :key="a.check_id">{{ a.critical ? '必守' : '参考' }}：{{ criterion(a.requirement) }}</li></ul>
          <section v-for="s in selected.sources" :key="s.id"><h4>{{ s.title }}</h4>
            <a :href="s.url.startsWith('https://') ? s.url : undefined" target="_blank" rel="noopener noreferrer">{{ s.url }}</a>
            <p>发布日期：{{ s.published || '未知' }}</p><p v-for="f in s.facts" :key="f.fact_id">{{ f.fact_id }}：{{ f.statement }}</p>
          </section>
          <details v-if="selected.fixture"><summary>合成场景定义（非实测轨迹）</summary><pre>{{ JSON.stringify(selected.fixture, null, 2) }}</pre></details>
          <p v-if="selected.case.legacy">原协议案例：{{ JSON.stringify(selected.case.legacy) }}</p>
        </article>
      </template>
      <template v-else-if="tab === 'run'">
        <h3>选择评测类型</h3><div class="areas">
          <article v-for="a in catalog.areas" :key="a.area"><h4>{{ a.label }}</h4><p>{{ a.availability }}</p>
            <template v-if="a.profile === 'protocol-1'">
              <p>固定20例 · 合成模型/搜索/嵌入 · 不调用真实供应商</p>
              <el-checkbox v-model="flags.rag" :disabled="authRole !== 'admin'">RAG</el-checkbox>
              <el-checkbox v-model="flags.memory" :disabled="authRole !== 'admin'">长期记忆</el-checkbox>
              <el-checkbox v-model="flags.review" :disabled="authRole !== 'admin'">审核</el-checkbox>
              <p><el-button type="primary" :disabled="!a.runnable || authRole !== 'admin'" :loading="starting" @click="startProtocol">运行离线协议评测</el-button></p>
            </template>
            <template v-else-if="a.profile === 'mechanism_regression'">
              <p>真实工程测试 · 替身供应商 · 不证明线上备用可用或报告质量</p>
              <el-select v-model="mechanismCases" multiple aria-label="机制回归案例" :disabled="authRole !== 'admin'">
                <el-option v-for="id in a.supported_case_ids || []" :key="id" :value="id" :label="`${id} ${catalog.cases.find(c => c.case_id === id)?.title || ''}`" />
              </el-select>
              <p><el-button :disabled="!a.runnable || authRole !== 'admin' || !mechanismCases.length" :loading="starting" @click="startRobustness">运行主备与恢复回归</el-button></p>
            </template>
            <el-button v-else disabled>执行器待接通</el-button>
          </article></div>
      </template>
      <template v-else-if="tab === 'records' || tab === 'compare'">
        <h3>{{ tab === 'compare' ? '对照实验' : '评测记录' }}</h3>
        <p v-if="tab === 'compare'">原协议只允许同配置下改变一个开关；跨版本与其他专项尚未接通。</p>
        <p v-if="historyError" role="alert">{{ historyError }} <el-button @click="loadHistory">重试</el-button></p>
        <el-table :data="history"><el-table-column prop="eval_id" label="评测编号" /><el-table-column prop="status" label="执行状态" />
          <el-table-column label="已归档/计划"><template #default="{ row }">{{ row.completed_cases }}/{{ row.total_cases }}</template></el-table-column>
          <el-table-column label="详情"><template #default="{ row }"><router-link :to="`/evaluation/protocol?eval=${encodeURIComponent(row.eval_id)}`">查看原协议结果与对照</router-link></template></el-table-column>
        </el-table><p v-if="!historyError && !history.length">尚无运行记录</p>
        <template v-if="tab === 'records'"><h4>主备与恢复机制回归</h4>
          <el-table :data="mechanismRuns"><el-table-column prop="run_id" label="运行编号" /><el-table-column label="执行状态"><template #default="{ row }">{{ runLabels[row.execution_status] }}</template></el-table-column>
            <el-table-column label="通过/计划"><template #default="{ row }">{{ row.summary.passed }}/{{ row.summary.planned }}</template></el-table-column>
            <el-table-column label="详情"><template #default="{ row }"><router-link :to="`/evaluation/runs/${row.run_id}`">查看实际回归结果</router-link></template></el-table-column>
          </el-table>
        </template>
      </template>
      <template v-else-if="tab === 'run-detail'">
        <h3>主备与恢复机制回归详情</h3><p>结果来自本地测试执行；报告质量尚未评判，真实供应商在线可用性尚未验证。</p>
        <template v-if="activeRun"><p>{{ activeRun.run_id }} · {{ runLabels[activeRun.execution_status] }} · 已处理 {{ activeRun.summary.processed }}/{{ activeRun.summary.planned }}</p>
          <p v-if="activeRun.error" role="alert">{{ activeRun.error }}</p>
          <el-table :data="activeRun.results"><el-table-column prop="case_id" label="案例" /><el-table-column label="机制判定"><template #default="{ row }">{{ checkLabels[row.check_status] }}</template></el-table-column><el-table-column prop="test_count" label="实际测试数" /><el-table-column prop="skipped" label="跳过数" /><el-table-column prop="duration_seconds" label="耗时（秒）" /><el-table-column prop="error" label="异常" /></el-table>
        </template>
      </template>
      <template v-else-if="tab === 'review'"><h3>人工复核</h3><p>题库核验与运行结果复核分开记录。当前尚未接通持久化复核，不能把待抽查案例自动标为人工通过。</p>
        <router-link to="/evaluation/datasets">先查看案例及来源依据</router-link></template>
    </template>
    <p><router-link to="/evaluation/protocol">原20例协议完整视图（兼容入口）</router-link></p>
  </section>
</template>

<style scoped>
.evaluation-center header p,.version{color:#667085}.evaluation-center nav{display:flex;gap:20px;flex-wrap:wrap;border-bottom:1px solid #dbe2ea;padding:12px 0;margin-bottom:20px}
nav a{color:#475467;text-decoration:none;padding-bottom:8px}nav a.current{color:#1769aa;border-bottom:2px solid #1769aa}
.areas{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin:20px 0}
article{background:white;border:1px solid #dbe2ea;border-radius:8px;padding:18px}article h3,article h4{margin-top:0}.filters{display:flex;gap:12px}.filters>*{max-width:280px}
.case-detail{margin-top:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{overflow-wrap:anywhere}[role=alert]{color:#b42318}
</style>
