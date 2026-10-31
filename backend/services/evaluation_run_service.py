"""专项评测应用服务；串行子进程、增量归档与关闭中断处理。"""
import asyncio
from datetime import datetime, timezone
import importlib.util
from uuid import uuid4

from db import evaluation_run_repository as repository
from evaluation import catalog
from evaluation.mechanism import MechanismRequest, MechanismResult, MechanismRun, TESTS, run_case
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


def start(body: MechanismRequest):
    global _handle,_run_id
    if _handle is not None and not _handle.done():
        raise EvaluationError('EVALUATION_RUNNING','已有专项评测正在运行',409)
    if not capabilities()['available']:
        raise EvaluationError('MECHANISM_ENGINE_UNAVAILABLE','本地pytest环境不可用，不能运行机制回归')
    pack=catalog.summary()
    from evaluation.archive import provenance
    metadata=provenance(pack['dataset_hash'],dict(rag=True,memory=True,review=True))
    metadata.update(profile=body.profile,engine='pytest_mechanism_regression',online_validated=False)
    run_id='er_'+uuid4().hex
    record=MechanismRun(run_id=run_id,profile=body.profile,execution_status='running',case_ids=body.case_ids,
                results=[],metadata=metadata,created_at=_now(),updated_at=_now(),
                error=None,report_quality='unknown',summary=dict(planned=len(body.case_ids),processed=0,passed=0,failed=0,unknown=0)).model_dump()
    try:
        repository.save(record)
    except OSError:
        raise EvaluationError('RUN_STORAGE_FAILED','专项评测归档不可写') from None
    _run_id=run_id
    _handle=asyncio.create_task(_execute(record))
    return record


async def _execute(record):
    try:
        for cid in record['case_ids']:
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
