"""版本归档与真实离线评测服务；不将预期结果填成实测结果。"""
import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

from db import evaluation_repository as repository, task_repository, usage_repository
from evaluation import metrics
from evaluation.offline import OfflineDependencies, execute_case
from evaluation.quality import aggregate, EvaluationResult
from evaluation.suite import load_suite, SUITE_VERSION
from services.memory_policy import PROMPT_VERSION


def provenance(dataset_hash, flags):
    root = Path(__file__).resolve().parents[2]
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode('utf-8').strip()
    try:
        commit = git('rev-parse', 'HEAD')
        tags = git('tag', '--points-at', 'HEAD').splitlines()
        dirty = bool(git('status', '--porcelain', '--untracked-files=no'))
        names = git('ls-files','--cached','--others','--exclude-standard','--','backend','frontend','mcp_servers','scripts','deploy','start.sh','start.bat').splitlines()
        code = hashlib.sha256()
        for name in sorted(set(names)):
            if name == 'scripts/verify_construction.py' or name.startswith('scripts/tests/'):
                continue
            path = root / name
            if path.is_file():
                code.update(name.encode()+b'\0'+hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).digest())
        source_hash = code.hexdigest()
    except (OSError, subprocess.CalledProcessError):
        commit, tags, dirty, source_hash = None, [], None, None
    return dict(mode='offline', suite_version=SUITE_VERSION, dataset_hash=dataset_hash,
        code_commit=commit, code_tag=tags[0] if tags else None, code_dirty=dirty, source_hash=source_hash,
        model=OfflineDependencies.model, prompt_version=PROMPT_VERSION,
        feature_flags=flags, randomness='固定外部响应与固定向量，无随机采样；耗时受本机负载影响',
        memory_protocol='每轮独立冷启动向量空间；按C01至C20顺序，可召回本轮先前合格案例，不能推论在线增益',
        evidence_scope='合成协议材料；有效引用率仅表示结构映射，不证明事实真实',
        online_experiment=dict(status='pending', results=None))


def collect(case, task_id, duration):
    from services.citations import check_citations
    task = task_repository.get_task(task_id)
    report = task.get('final_report') or ''
    check = check_citations(task_id, report, task['evidence'])
    citations = check['citations']
    rows = [r for r in usage_repository.list_usage(task_id) if r['status'] != 'budget_exceeded']
    known = bool(rows) and all(r['usage_known'] for r in rows)
    costs = {}
    for row in rows:
        if row['estimated_cost'] is not None:
            costs[row['currency']] = costs.get(row['currency'],0.0) + row['estimated_cost']
    execution = task['execution_status'] == 'completed'
    quality = execution and task['quality_status'] == 'passed' and not task['forced_output']
    category = None if quality else task['error_code'] or ('REVIEW_DISABLED' if task['quality_status']=='not_run' and execution else 'QUALITY_NOT_PASSED')
    if not execution and category == 'DRAFT_UNAVAILABLE':
        # 仅由实际调用账本细分依赖失败，不能按案例预期填失败分类。
        failures = [r['error_code'] for r in rows if r['status']=='failed' and r['error_code']]
        if failures:
            category = failures[-1]
    return EvaluationResult(**dict(case_id=case['case_id'],case_index=int(case['case_id'][1:]),category=case['category'],scenario=case['scenario'],topic=case['topic'],keywords=case['keywords'],task_id=task_id,
        success=quality,execution_success=execution,quality_passed=quality,quality_status=task['quality_status'],forced_output=bool(task['forced_output']),forced_pass=bool(task['forced_pass']),
        failure_category=category,
        duration_seconds=round(duration,4),react_loops=metrics.count_react_actions(task_repository.get_logs(task_id)),keyword_coverage=metrics.compute_keyword_coverage(report,case['keywords']),memory_hits=len(task['memory_hits']),
        citation_count=len(citations),citation_valid_count=sum(c['valid'] for c in citations),citation_valid_rate=sum(c['valid'] for c in citations)/len(citations) if citations else None,
        input_tokens=sum(r['input_tokens'] for r in rows) if known else None,output_tokens=sum(r['output_tokens'] for r in rows) if known else None,
        estimated_costs=costs,cost_complete=bool(rows) and all(r['estimated_cost'] is not None for r in rows),
        report_sha256=hashlib.sha256(report.encode()).hexdigest() if report else None,
        error=None if execution else task['error'],manual_fact_check=dict(status='pending',sample_size=0,accuracy=None))).model_dump()


def summarize(results, total):
    legacy = metrics.compute_metrics(results)
    legacy['summary']['metric_version'] = 3
    legacy['quality'] = aggregate(results,total)
    return legacy


async def execute_suite(eval_id, cases, flags):
    results = []
    try:
        with TemporaryDirectory(prefix='smartbrief-protocol-', ignore_cleanup_errors=True) as directory:
            for case in cases:
                started = perf_counter()
                task_id = await execute_case(OfflineDependencies(case,directory,flags))
                results.append(collect(case,task_id,perf_counter()-started))
                # 部分结果与进度、指标同事务；中断仍可读取真实已完成部分。
                repository.save_partial(eval_id,results,summarize(results,len(cases)))
        data = summarize(results,len(cases))
        report = metrics.build_markdown(eval_id,results,data)
        report += '\n\n## 离线协议边界\n\n合成模型、搜索与嵌入；真实图/预算/状态/存储。以下是软件协议成绩，不代表在线模型效果。\n\n'
        report += '```json\n'+json.dumps(data['quality'],ensure_ascii=False,indent=2)+'\n```\n\n人工事实抽查：pending；在线实验：pending。\n'
        repository.finish(eval_id,results,data,report)
    except Exception:
        repository.fail(eval_id,'评测执行或持久化失败；已完成部分保留')
        raise


async def run_offline(flags=None):
    flags = flags or dict(rag=True,memory=True,review=True)
    cases, digest = load_suite()
    eval_id = 'eval_' + uuid4().hex
    repository.create(eval_id,len(cases),metadata=provenance(digest,flags))
    await execute_suite(eval_id,cases,flags)
    return repository.get(eval_id)
