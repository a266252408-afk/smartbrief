"""专项评测应用服务；串行子进程、增量归档与关闭中断处理。"""
import asyncio
from datetime import datetime, timezone
import importlib.util
from uuid import uuid4

from db import evaluation_run_repository as repository
from evaluation import catalog
from evaluation.mechanism import MechanismRequest, MechanismResult, TESTS, run_case
from evaluation.search_replay import SearchRequest, SearchResult, LiveSearchResult, EvaluationRun, run_case as replay_case
from evaluation.full_task import FullTaskRequest, FullTaskResult, run_case as full_case
from evaluation.authorization import reserve, reserve_search, EvaluationGrant, SearchEvaluationGrant
from evaluation.search_live import run_case as live_search_case
from evaluation.experiment import ExperimentRequest, ExperimentCaseResult, validate_sources, run_case as experiment_case
from services.evaluation_service import EvaluationError

_handle=None
_run_id=None


def _now():
    return datetime.now(timezone.utc).isoformat()


def capabilities():
    return dict(profile='mechanism_regression',supported_case_ids=list(TESTS),
                available=importlib.util.find_spec('pytest') is not None and all(
                    (catalog.PACK_ROOT.parents[2]/'backend/tests'/name).is_file() for name,_ in TESTS.values()),
                scope='本地白名单工程回归；真实路由与存储、替身供应商，非在线效果')


def start(body: MechanismRequest | SearchRequest | FullTaskRequest | ExperimentRequest):
    global _handle,_run_id
    if _handle is not None and not _handle.done():
        raise EvaluationError('EVALUATION_RUNNING','已有专项评测正在运行',409)
    grant = reserve_search(body) if body.profile == 'search_live' else reserve(body) if body.profile == 'full_task_live' else None
    if body.profile == 'bounded_experiment':
        try:
            validate_sources(body.case_ids)
        except ValueError:
            raise EvaluationError('EXPERIMENT_DEVELOPMENT_ONLY', '仅开发组可用于有限实验', 422) from None
    if body.profile=='mechanism_regression' and not capabilities()['available']:
        raise EvaluationError('MECHANISM_ENGINE_UNAVAILABLE','本地pytest环境不可用，不能运行机制回归')
    pack=catalog.summary()
    from evaluation.archive import provenance
    metadata=provenance(pack['dataset_hash'],dict(rag=True,memory=True,review=True))
    metadata.update(profile=body.profile,engine='pytest_mechanism_regression',online_validated=False)
    metadata.update(model='per-test-fixture',feature_flags=None,
                    randomness='固定工程测试条件，具体主备以实际调用轨迹为准')
    if body.profile=='search_replay':
        metadata.update(model='none',engine='frozen_search_replay',
                        evidence_scope='冻结资料卡或合成响应，不是当次联网结果',randomness='固定响应，仅耗时受本机负载影响')
    if body.profile == 'search_live':
        metadata.update(mode='online', engine='exa_live_search', model='none',
            authorized_search_grant=grant.model_dump(), memory_protocol='不读写业务记忆',
            evidence_scope='当次提供商摘要；语义/正文/统计期未知，非研究智能体自主检索',
            randomness='固定公开查询和前5条；网页/供应商返回随时间变化，不作严格归因')
    if body.profile in ('full_task_local', 'full_task_live'):
        metadata.update(engine='isolated_full_workflow', model_group=body.group,
                        model=grant.model if grant else 'fixture-primary/fixture-backup',
                        mode='online' if grant else 'offline', memory_protocol='每案例独立冷启动；RAG/长期记忆禁用',
                        evidence_scope='冻结公开资料；独立语义质量未评判',
                        authorized_grant=grant.model_dump() if grant else None,
                        feature_flags=dict(rag=False, memory=False, review=True))
        metadata['execution_budget'] = (dict(max_model_calls=grant.max_model_calls, max_input_bytes=grant.max_input_bytes,
            max_output_tokens=grant.max_output_tokens, estimated_cost_ceiling=grant.estimated_cost_ceiling,
            currency=grant.currency) if grant else dict(deadline_seconds=180, max_task_tokens=500000,
                completion_reserve=2048, per_call_timeout_seconds=30, model_max_retries=0))
        if not grant:
            metadata['execution_budget']['max_provider_attempts'] = 10
    if body.profile == 'bounded_experiment':
        metadata.update(engine='bounded_public_feedback_1', model='fixture-primary/fixture-backup',
            model_group='primary', feature_flags=dict(rag=False, memory=False, review=True),
            memory_protocol='每次尝试独立冷启动，不写长期记忆；反馈仅单次上下文',
            evidence_scope='开发组公开输入；实际初始SDK故障，非线上事故；独立质量未知',
            experiment_config=dict(max_attempts=body.max_attempts, call_limit=body.call_limit,
                                   feedback_scope='public_execution_state_only', initial_fault='both_unavailable'),
            experiment_progress=[])
    run_id='er_'+uuid4().hex
    record=EvaluationRun(run_id=run_id,profile=body.profile,execution_status='running',case_ids=body.case_ids,
                results=[],metadata=metadata,created_at=_now(),updated_at=_now(),
                error=None,report_quality='unknown',summary=dict(planned=len(body.case_ids),processed=0,passed=0,failed=0,unknown=0)).model_dump()
    try:
        repository.save(record)
    except OSError:
        raise EvaluationError('RUN_STORAGE_FAILED','专项评测归档不可写') from None
    _run_id=run_id
    _handle=asyncio.create_task(_execute(record))
    return record


def start_judging(body):
    global _handle,_run_id
    if _handle is not None and not _handle.done():
        raise EvaluationError('EVALUATION_RUNNING','已有专项评测正在运行',409)
    from services.evaluation_judging_service import prepare
    record=prepare(body)
    try:
        repository.save(record)
    except OSError:
        raise EvaluationError('RUN_STORAGE_FAILED','评分归档不可写，已停止调用') from None
    _run_id=record['run_id']
    _handle=asyncio.create_task(_execute(record))
    return record


async def _execute(record):
    try:
        if record['profile']=='independent_judging':
            from services.evaluation_judging_service import execute
            await execute(record)
            return
        for cid in record['case_ids']:
            if record['profile'] == 'search_live':
                result = LiveSearchResult.model_validate(await live_search_case(cid,
                    SearchEvaluationGrant.model_validate(record['metadata']['authorized_search_grant']))).model_dump()
            elif record['profile'] == 'bounded_experiment':
                async def update(progress):
                    record['metadata']['experiment_progress'].append(progress)
                    record['updated_at'] = _now()
                    repository.save(record)
                result = ExperimentCaseResult.model_validate(await experiment_case(cid,
                    repository.root()/'work'/record['run_id']/cid,
                    **{k:record['metadata']['experiment_config'][k] for k in ('max_attempts','call_limit')},
                    on_update=update)).model_dump()
            elif record['profile'] in ('full_task_local', 'full_task_live'):
                raw_grant = record['metadata'].get('authorized_grant')
                grant = EvaluationGrant.model_validate(raw_grant) if raw_grant else None
                result=FullTaskResult.model_validate(await full_case(cid, record['metadata']['model_group'],
                    repository.root()/'work'/record['run_id']/cid, grant)).model_dump()
            elif record['profile']=='search_replay':
                result=SearchResult.model_validate(await replay_case(cid)).model_dump()
            else:
                result=MechanismResult.model_validate(await run_case(cid,repository.root()/'work'/record['run_id']/cid)).model_dump()
            record['results'].append(result)
            record['updated_at']=_now()
            record['summary']=dict(planned=len(record['case_ids']),processed=len(record['results']),
                                   **{key:sum(r['check_status']==value for r in record['results'])
                                      for key,value in [('passed','pass'),('failed','fail'),('unknown','unknown')]})
            repository.save(record)
        record['execution_status']='completed'
    except asyncio.CancelledError:
        record['execution_status']='failed'
        record['error']='服务关闭，专项评测中断；已完成部分保留'
    except Exception:
        record['execution_status']='failed'
        record['error']='专项执行或归档失败；不能据此宣称通过'
    finally:
        record['updated_at']=_now()
        repository.save(record)


def get(run_id):
    try:
        record=repository.get(run_id)
        if record is None:
            raise EvaluationError('RUN_NOT_FOUND','专项评测不存在',404)
        return record
    except (OSError,ValueError,KeyError):
        raise EvaluationError('RUN_STORAGE_FAILED','专项评测归档不可读') from None


def list_runs():
    try:
        return dict(items=repository.list_runs())
    except (OSError,ValueError):
        raise EvaluationError('RUN_STORAGE_FAILED','专项评测归档不可读') from None


def interrupt_orphans():
    for record in repository.list_runs():
        if record['execution_status']=='running':
            record.update(execution_status='failed',error='服务重启，旧专项运行中断，已完成部分保留',updated_at=_now())
            repository.save(record)


async def close():
    global _handle,_run_id
    if _handle is not None and not _handle.done():
        _handle.cancel()
        await asyncio.gather(_handle,return_exceptions=True)
        # 尚未进入_execute时被取消，也必须让已创建的记录脱离running。
        record=get(_run_id)
        if record['execution_status']=='running':
            record.update(execution_status='failed',error='服务关闭，专项评测未执行',updated_at=_now())
            repository.save(record)
    _handle=None
    _run_id=None
