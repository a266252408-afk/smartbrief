<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { createComparison, fetchComparisons, type ComparisonResult, startFullTask, gradeReport, calibrateGrader, type CalibrationSummary, fetchCatalog, fetchCatalogCase, startMechanism, startSearchReplay, fetchMechanismRuns, fetchMechanismRun, saveEvaluationReview, fetchEvaluationReviews, type MechanismResult, type EvaluationReview, type MechanismRun, type EvaluationCatalog, type CatalogDetail, type CatalogCase } from '../api/evaluationCenter'
import { fetchEvalReports, runEvaluation, type EvalReportSummary } from '../api/evaluation'
import { authRole } from '../api/session'

const route = useRoute(), router = useRouter()
const catalog = ref<EvaluationCatalog | null>(null), error = ref(''), loading = ref(false)
const selected = ref<CatalogDetail | null>(null), detailError = ref(''), detailLoading = ref(false)
const area = ref(''), collection = ref(''), query = ref('')
const history = ref<EvalReportSummary[]>([]), historyError = ref(''), starting = ref(false)
const flags = ref({ rag: true, memory: true, review: true })
const mechanismCases = ref(['R009', 'G023']), mechanismRuns = ref<MechanismRun[]>([]), mechanismError = ref('')
const searchCases = ref(['G001'])
const baselineId = ref(''), currentId = ref(''), changedDimension = ref<'code' | 'model_group'>('code')
const comparisonRows = ref<ComparisonResult[]>([]), activeComparison = ref<ComparisonResult | null>(null), comparisonError = ref('')
const compareRuns = computed(() => mechanismRuns.value.filter(r => r.profile === 'full_task_local'))
const fullCases = ref(['G017']), modelGroup = ref<'primary' | 'backup' | 'mixed'>('primary')
const reportCase = ref('G002'), reportText = ref(''), calibration = ref<CalibrationSummary | null>(null)
const gradingError = ref('')
const activeRun = ref<MechanismRun | null>(null)
const traceRows = ref<Record<string, unknown>[] | null>(null)
const evidenceRows = ref<NonNullable<MechanismResult['evidence']>>([])
const reviewRows = ref<EvaluationReview[]>([]), reviewNotes = ref(''), reviewDecision = ref<'pass' | 'fail' | 'unknown'>('unknown')
const reviewCaseId = ref(''), reviewing = ref(false), reviewError = ref(''), reviewMessage = ref('')
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
const reviewLabels: Record<string, string> = { pending: '待抽查', pass: '核验通过', fail: '需修改', unknown: '已抽查待确认' }
function criterion(text: string) { return text.replace('oracle.required_fact_ids', '参考事实清单') }
function traceLabel(event: Record<string, unknown>): string {
  if (event.kind === 'search_request') return `回放查询：${event.query}（未联网）`
  if (event.kind === 'search_result') return `回放结果：${event.title}；发布日期：${event.published_at || '未知'}；统计期：${event.data_period || '未知'}`
  if (event.kind === 'model_call') return `${event.model_role === 'primary' ? '主模型' : event.model_role === 'backup' ? '备用模型' : '模型'}调用${event.status === 'success' ? '成功' : '未成功'}${event.error_code ? `（${event.error_code}）` : ''}，耗时 ${event.duration_ms ?? '未知'} 毫秒`
  if (event.kind === 'checkpoint_before_interrupt') return `中断前已保存步骤：${JSON.stringify(event.committed)}；中断位置：${event.interrupted || '当前单元'}`
  if (event.kind === 'recovery_result') return `恢复结果：${event.resume_mode === 'checkpoint' ? '检查点续跑' : String(event.resume_mode || '未知')}，已完成步骤：${JSON.stringify(event.completed_steps)}`
  const agents: Record<string, string> = { planner: '任务规划', researcher: '资料研究', 'writer:A': '撰写章节A', 'writer:B': '撰写章节B', reviewer: '审核' }
  return agents[String(event.label)] || '执行事件'
}
function showProcess(row: MechanismResult) { traceRows.value = row.trace || []; evidenceRows.value = row.evidence || [] }
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
async function loadReviews() {
  reviewError.value = ''
  try { reviewRows.value = await fetchEvaluationReviews() }
  catch { reviewError.value = '人工核验记录读取失败，请重试' }
}
async function submitReview(target: 'case_definition' | 'run_result') {
  if (!catalog.value || reviewing.value || authRole.value !== 'admin' || !reviewNotes.value.trim()) return
  const id = target === 'case_definition' ? selected.value?.case.case_id : reviewCaseId.value
  const run = target === 'run_result' ? activeRun.value : null
  if (!id || (run && !run.metadata?.dataset_hash)) return
  reviewing.value = true; reviewError.value = ''; reviewMessage.value = ''
  try {
    await saveEvaluationReview({ target, case_id: id, run_id: run?.run_id || null,
      dataset_hash: run?.metadata?.dataset_hash || catalog.value.dataset_hash,
      decision: reviewDecision.value, notes: reviewNotes.value })
    reviewMessage.value = '人工核验已保存；原始机器成绩和题库内容未改写'
    reviewNotes.value = ''; reviewDecision.value = 'unknown'
    await load()
    if (tab.value === 'review') await loadReviews()
  } catch { reviewError.value = '核验未保存，请检查资料版本、备注或访问权限' }
  finally { reviewing.value = false }
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
async function startSearch() {
  if (starting.value || authRole.value !== 'admin') return
  starting.value = true; mechanismError.value = ''
  try { const data = await startSearchReplay(searchCases.value); await router.push(`/evaluation/runs/${encodeURIComponent(data.run_id)}`) }
  catch { mechanismError.value = '搜索回放未启动，请检查所选案例或是否已有运行' }
  finally { starting.value = false }
}
async function submitReport() {
  if (starting.value || authRole.value !== 'admin') return
  starting.value = true; gradingError.value = ''
  try { const data = await gradeReport(reportCase.value, reportText.value); await router.push(`/evaluation/runs/${data.run_id}`) }
  catch { gradingError.value = '独立评分未保存，请检查案例和输入后重试' }
  finally { starting.value = false }
}
async function runCalibration() {
  if (starting.value || authRole.value !== 'admin') return
  starting.value = true; gradingError.value = ''
  try { calibration.value = await calibrateGrader() }
  catch { gradingError.value = '评分器校准读取失败，请重试' }
  finally { starting.value = false }
}
async function startFull() {
  if (starting.value || authRole.value !== 'admin' || !fullCases.value.length) return
  starting.value = true; mechanismError.value = ''
  try { const data = await startFullTask(fullCases.value, modelGroup.value); await router.push(`/evaluation/runs/${data.run_id}`) }
  catch { mechanismError.value = '完整任务未启动，请检查案例和运行状态' }
  finally { starting.value = false }
}
async function loadComparisons() {
  comparisonError.value = ''
  try { comparisonRows.value = await fetchComparisons() }
  catch { comparisonError.value = '专项对照归档不可读，请重试' }
}
async function submitComparison() {
  if (starting.value || authRole.value !== 'admin' || !baselineId.value || !currentId.value || baselineId.value === currentId.value) return
  starting.value = true; comparisonError.value = ''
  try { activeComparison.value = await createComparison({ baseline_id: baselineId.value, current_id: currentId.value, changed_dimension: changedDimension.value }); await loadComparisons() }
  catch { comparisonError.value = '对照未保存，请检查运行编号或重试' }
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
  if (['records', 'compare'].includes(tab.value)) void loadMechanisms()
  if (tab.value === 'compare') void loadComparisons()
  if (tab.value === 'run-detail') void loadRun()
  if (tab.value === 'review') void loadReviews()
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
    <p v-if="reviewError" role="alert">{{ reviewError }}</p><p v-if="reviewMessage" role="status">{{ reviewMessage }}</p>
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
          <el-table-column label="人工核验" width="130"><template #default="{ row }">{{ reviewLabels[row.human_review] || '待抽查' }}</template></el-table-column>
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
          <section class="review-form"><h4>核验这道题和参考依据</h4><p>这里只记录你检查过的案例，不代表系统已经运行通过。</p>
            <el-select v-model="reviewDecision" aria-label="题目核验结论" :disabled="authRole !== 'admin'"><el-option label="核验通过" value="pass" /><el-option label="需要修改" value="fail" /><el-option label="无法确认" value="unknown" /></el-select>
            <el-input v-model="reviewNotes" type="textarea" aria-label="核验备注" placeholder="写明核对内容或发现的问题" :maxlength="2000" :disabled="authRole !== 'admin'" />
            <el-button :disabled="authRole !== 'admin' || !reviewNotes.trim()" :loading="reviewing" @click="submitReview('case_definition')">保存题目核验</el-button>
          </section>
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
            <template v-else-if="a.profile === 'search_replay'">
              <p>冻结资料与响应解析；不调用真实搜索或模型，语义质量另行评判。</p>
              <el-select v-model="searchCases" multiple aria-label="搜索回放案例" :disabled="authRole !== 'admin'"><el-option v-for="id in a.supported_case_ids || []" :key="id" :value="id" :label="`${id} ${catalog.cases.find(c => c.case_id === id)?.title || ''}`" /></el-select>
              <p><el-button :disabled="!a.runnable || authRole !== 'admin' || !searchCases.length" :loading="starting" @click="startSearch">运行搜索回放</el-button></p>
              <p>真实联网：等待调用量、费用与数据去向授权。</p>
            </template>
            <template v-else-if="a.profile === 'report_grading'">
              <p>提交实际报告，按当前固定资料独立检查。引用使用资料编号，如 [S01]；语义模型未启用时保留未知。</p>
              <el-select v-model="reportCase" aria-label="报告评分案例" :disabled="authRole !== 'admin'"><el-option v-for="id in a.supported_case_ids || []" :key="id" :value="id" :label="id" /></el-select>
              <el-input v-model="reportText" type="textarea" aria-label="待评报告" :maxlength="100000" :disabled="authRole !== 'admin'" />
              <p><el-button :disabled="authRole !== 'admin'" :loading="starting" @click="submitReport">独立评分并归档</el-button></p>
              <el-button :disabled="authRole !== 'admin'" :loading="starting" @click="runCalibration">运行评分器校准</el-button>
            </template>
            <template v-else-if="a.profile === 'full_task_local'">
              <p>真实完整工作流；模型供应商为替身，资料冻结，记忆隔离。完成状态不代表报告质量。</p>
              <el-select v-model="fullCases" multiple aria-label="完整任务案例" :disabled="authRole !== 'admin'"><el-option v-for="id in a.supported_case_ids || []" :key="id" :value="id" :label="id" /></el-select>
              <el-select v-model="modelGroup" aria-label="模型运行组" :disabled="authRole !== 'admin'"><el-option label="主模型" value="primary" /><el-option label="固定备用" value="backup" /><el-option label="主故障后接管" value="mixed" /></el-select>
              <p><el-button :disabled="authRole !== 'admin' || !fullCases.length" :loading="starting" @click="startFull">运行完整任务替身基线</el-button></p>
              <p>真实模型：等待服务端单次授权和预算，页面不能自行授权。</p>
            </template>
            <el-button v-else disabled>执行器待接通</el-button>
          </article></div>
        <p v-if="gradingError" role="alert">{{ gradingError }}</p>
        <section v-if="calibration"><h3>评分器校准结果</h3><p>校准样本 {{ calibration.total }}；未知 {{ calibration.unknown }}；误放行 {{ calibration.false_accepts }}；误拒绝 {{ calibration.false_rejects }}；准确率 {{ calibration.accuracy === null ? '待真实校准' : calibration.accuracy }}</p>
          <p>这是评分器样本，不能作为被测系统成绩。留出资料仅用于验收。</p><router-link v-if="calibration.run_id" :to="`/evaluation/runs/${calibration.run_id}`">查看校准归档与人工复核</router-link>
          <el-table :data="calibration.results"><el-table-column prop="case_id" label="样本" /><el-table-column prop="split" label="资料分组" /><el-table-column prop="expected" label="参考" /><el-table-column prop="label" label="判分" /><el-table-column prop="reason" label="依据" /></el-table>
        </section>
      </template>
      <template v-else-if="tab === 'records' || tab === 'compare'">
        <h3>{{ tab === 'compare' ? '对照实验' : '评测记录' }}</h3>
        <p v-if="tab === 'compare'">原协议保留单开关规则；专项跨版本比较需明确声明变化，并核对全部冻结条件。</p>
        <p v-if="historyError" role="alert">{{ historyError }} <el-button @click="loadHistory">重试</el-button></p>
        <el-table :data="history"><el-table-column prop="eval_id" label="评测编号" /><el-table-column prop="status" label="执行状态" />
          <el-table-column label="已归档/计划"><template #default="{ row }">{{ row.completed_cases }}/{{ row.total_cases }}</template></el-table-column>
          <el-table-column label="详情"><template #default="{ row }"><router-link :to="`/evaluation/protocol?eval=${encodeURIComponent(row.eval_id)}`">查看原协议结果与对照</router-link></template></el-table-column>
        </el-table><p v-if="!historyError && !history.length">尚无运行记录</p>
        <template v-if="tab === 'records'"><h4>专项评测记录</h4>
          <el-table :data="mechanismRuns"><el-table-column prop="run_id" label="运行编号" /><el-table-column label="执行状态"><template #default="{ row }">{{ runLabels[row.execution_status] }}</template></el-table-column>
            <el-table-column label="类型"><template #default="{ row }">{{ row.profile?.startsWith('full_task_') ? '完整任务' : row.profile === 'grader_calibration' ? '评分器校准' : row.profile === 'report_grading' ? '独立报告评分' : row.profile === 'search_replay' ? '搜索回放' : '机制回归' }}</template></el-table-column>
            <el-table-column label="通过/计划"><template #default="{ row }">{{ row.profile === 'search_replay' ? '语义待评判' : `${row.summary.passed}/${row.summary.planned}` }}</template></el-table-column>
            <el-table-column prop="summary.unknown" label="待确认/未评分" />
            <el-table-column label="详情"><template #default="{ row }"><router-link :to="`/evaluation/runs/${row.run_id}`">查看实际回归结果</router-link></template></el-table-column>
          </el-table>
        </template>
        <section v-if="tab === 'compare'"><h3>完整任务专项对照</h3><p>完整保留成功、失败和未知案例；不可比时只并排，不计算增益。</p>
          <el-select v-model="baselineId" aria-label="专项基线运行"><el-option v-for="r in compareRuns" :key="r.run_id" :value="r.run_id" :label="`${r.run_id} ${r.metadata?.model_group || ''}`" /></el-select>
          <el-select v-model="currentId" aria-label="专项当前运行"><el-option v-for="r in compareRuns" :key="r.run_id" :value="r.run_id" :label="`${r.run_id} ${r.metadata?.model_group || ''}`" /></el-select>
          <el-select v-model="changedDimension" aria-label="声明变化维度"><el-option label="源码版本" value="code" /><el-option label="模型运行组" value="model_group" /></el-select>
          <p><el-button :disabled="authRole !== 'admin' || !baselineId || !currentId || baselineId === currentId" :loading="starting" @click="submitComparison">保存专项对照</el-button></p>
          <p v-if="comparisonError" role="alert">{{ comparisonError }}</p>
          <article v-if="activeComparison" data-testid="comparison-result"><h4>{{ activeComparison.attribution_allowed ? '满足协议可比条件' : '仅并排，不可归因' }}</h4>
            <p>{{ activeComparison.interpretation }}</p><ul><li v-for="reason in activeComparison.reasons" :key="reason">{{ reason }}</li></ul>
            <p>执行通过率差值：{{ activeComparison.execution_pass_rate_delta ?? '不可计算' }}；独立质量差值：{{ activeComparison.quality_pass_rate_delta ?? '尚未独立评分' }}；全部尝试耗时差值：{{ activeComparison.all_attempt_seconds_delta ?? '未知' }}</p>
            <el-table :data="activeComparison.case_changes"><el-table-column prop="case_id" label="案例" /><el-table-column prop="baseline" label="基线" /><el-table-column prop="current" label="当前" /><el-table-column prop="transition" label="变化" /></el-table>
          </article>
          <el-table :data="comparisonRows"><el-table-column prop="comparison_id" label="对照编号" /><el-table-column label="状态"><template #default="{ row }">{{ row.attribution_allowed ? '协议可比' : '仅并排' }}</template></el-table-column><el-table-column label="详情"><template #default="{ row }"><el-button link @click="activeComparison = row">查看专项对照</el-button></template></el-table-column></el-table>
        </section>
      </template>
      <template v-else-if="tab === 'run-detail'">
        <h3>{{ activeRun?.profile?.startsWith('full_task_') ? '完整任务运行详情' : activeRun?.profile === 'grader_calibration' ? '评分器校准详情' : activeRun?.profile === 'report_grading' ? '独立报告评分详情' : activeRun?.profile === 'search_replay' ? '冻结搜索回放详情' : '主备与恢复机制回归详情' }}</h3><p v-if="activeRun?.profile !== 'report_grading' && activeRun?.profile !== 'grader_calibration'">报告质量尚未评判，真实供应商在线可用性尚未验证；冻结回放不代表自主搜索效果。</p><p v-else>独立判分与评分器校准分别展示；真实模型效果仍待授权验证。</p>
        <template v-if="activeRun"><p>{{ activeRun.run_id }} · {{ runLabels[activeRun.execution_status] }} · 已处理 {{ activeRun.summary.processed }}/{{ activeRun.summary.planned }}</p>
          <p v-if="activeRun.error" role="alert">{{ activeRun.error }}</p>
          <section v-if="activeRun.profile?.startsWith('full_task_')"><p>工作流完成、业务审核、独立报告质量分别记录；替身模型结果不能当成真实模型效果。</p>
            <el-table :data="activeRun.results"><el-table-column prop="case_id" label="案例" /><el-table-column prop="task_execution_status" label="工作流状态" /><el-table-column prop="business_quality" label="业务审核" /><el-table-column label="独立报告质量"><template #default>待独立评判</template></el-table-column><el-table-column prop="calls" label="账本调用数" /><el-table-column label="结果"><template #default="{ row }"><el-button link @click="showProcess(row)">查看完整任务过程</el-button></template></el-table-column></el-table>
            <article v-for="r in activeRun.results" :key="r.case_id"><h4>{{ r.case_id }} 报告</h4><pre>{{ r.report }}</pre><p v-if="r.error" role="alert">{{ r.error }}</p></article>
          </section>
          <section v-else-if="activeRun.profile === 'grader_calibration'"><p>校准样本不计被测系统质量；未知项需另行授权真实校准。</p>
            <el-table :data="activeRun.results"><el-table-column prop="case_id" label="样本" /><el-table-column prop="split" label="资料分组" /><el-table-column prop="expected" label="参考" /><el-table-column prop="label" label="判分" /><el-table-column prop="reason" label="依据" /></el-table>
          </section>
          <section v-else-if="activeRun.profile === 'report_grading'"><p>独立评分不改变业务任务的原始质量状态；缺失语义判分为未知。</p>
            <article v-for="r in activeRun.results" :key="r.case_id"><h4>{{ r.case_id }} · {{ checkLabels[r.check_status] }}</h4><p>报告指纹：{{ r.report_sha256 }}</p><pre>{{ r.report }}</pre>
              <el-table :data="r.judgements"><el-table-column prop="rubric_id" label="检查" /><el-table-column prop="label" label="结论" /><el-table-column prop="reason" label="依据" /></el-table>
            </article></section>
          <el-table v-else-if="activeRun.profile !== 'search_replay'" :data="activeRun.results"><el-table-column prop="case_id" label="案例" /><el-table-column label="机制判定"><template #default="{ row }">{{ checkLabels[row.check_status] }}</template></el-table-column><el-table-column prop="test_count" label="实际测试数" /><el-table-column prop="skipped" label="跳过数" /><el-table-column prop="duration_seconds" label="耗时（秒）" /><el-table-column prop="error" label="异常" /><el-table-column label="过程"><template #default="{ row }"><el-button link @click="showProcess(row)">查看过程</el-button></template></el-table-column></el-table>
          <el-table v-else :data="activeRun.results"><el-table-column prop="case_id" label="案例" /><el-table-column prop="metrics.returned_count" label="回放结果数" /><el-table-column prop="metrics.unique_sources" label="去重后来源数" /><el-table-column prop="metrics.unknown_period_documents" label="统计期未知" /><el-table-column label="过期数"><template #default="{ row }">{{ row.metrics?.stale_documents ?? '未指定窗口' }}</template></el-table-column><el-table-column label="语义质量"><template #default>待独立评判</template></el-table-column><el-table-column label="过程与资料"><template #default="{ row }"><el-button link @click="showProcess(row)">查看回放资料</el-button></template></el-table-column></el-table>
          <section v-if="traceRows !== null"><h4>实际执行事件</h4><p v-if="!traceRows.length">该记录没有可用轨迹证据，不补造过程。</p><ol><li v-for="(event, index) in traceRows" :key="index">{{ traceLabel(event) }}</li></ol></section>
          <article v-for="(doc, index) in evidenceRows" :key="index"><h4>{{ doc.title }}</h4><a v-if="doc.url?.startsWith('https://') || doc.url?.startsWith('http://')" :href="doc.url || undefined" target="_blank" rel="noopener noreferrer">查看原始来源</a><p>{{ doc.snippet || doc.excerpt }}</p><p>发布日期：{{ doc.published_at || '未知' }}；统计期：{{ doc.data_period || '未知' }}；{{ doc.content_available ? '回放片段可用' : '内容不可用' }}</p></article>
          <section class="review-form"><h4>人工复核本次案例结果</h4><p>记录人工意见，原始机制判定保留。</p>
            <el-select v-model="reviewCaseId" aria-label="复核运行案例" :disabled="authRole !== 'admin'"><el-option v-for="r in activeRun.results" :key="r.case_id" :value="r.case_id" :label="r.case_id" /></el-select>
            <el-select v-model="reviewDecision" aria-label="运行复核结论" :disabled="authRole !== 'admin'"><el-option label="复核通过" value="pass" /><el-option label="复核不通过" value="fail" /><el-option label="无法确认" value="unknown" /></el-select>
            <el-input v-model="reviewNotes" type="textarea" aria-label="核验备注" :maxlength="2000" :disabled="authRole !== 'admin'" />
            <el-button :disabled="authRole !== 'admin' || !reviewNotes.trim() || !reviewCaseId || !activeRun.metadata?.dataset_hash" :loading="reviewing" @click="submitReview('run_result')">保存结果复核</el-button>
          </section>
        </template>
      </template>
      <template v-else-if="tab === 'review'"><h3>人工复核记录</h3><p>题目核验与运行结果复核分开，所有修改保留先前记录；未抽查案例保持待抽查。</p>
        <router-link to="/evaluation/datasets">查看案例及来源后提交核验</router-link>
        <el-table :data="reviewRows"><el-table-column prop="case_id" label="案例" width="95" /><el-table-column label="核验对象"><template #default="{ row }">{{ row.target === 'case_definition' ? '题目与参考依据' : '运行结果' }}</template></el-table-column>
          <el-table-column label="结论"><template #default="{ row }">{{ reviewLabels[row.decision] }}</template></el-table-column><el-table-column prop="notes" label="备注" min-width="260" /><el-table-column prop="recorded_at" label="记录时间" />
        </el-table><p v-if="!reviewRows.length && !reviewError">尚无人工核验记录</p>
      </template>
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
.review-form{border-top:1px solid #dbe2ea;padding-top:16px;margin-top:20px;display:grid;gap:12px}.review-form .el-select{max-width:300px}.review-form .el-button{justify-self:start}
</style>
