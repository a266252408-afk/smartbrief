"""评测应用服务：输入、并发、离线执行、版本可比性与异常边界。"""
import asyncio
from uuid import uuid4
from typing import Literal

from pydantic import BaseModel, ConfigDict
from db import evaluation_repository as repository
from evaluation import archive, runner
from evaluation.suite import load_suite


class FeatureFlags(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    rag: bool
    memory: bool
    review: bool


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    suite_version: str
    mode: Literal['offline','online']
    feature_flags: FeatureFlags


class EvaluationError(RuntimeError):
    def __init__(self,code,message,status=503):
        super().__init__(message)
        self.code,self.status = code,status


def start(body):
    if body.mode != 'offline':
        raise EvaluationError('ONLINE_EVALUATION_NOT_AUTHORIZED','在线实验pending，需单独批准数据去向与预算后实施',403)
    try:
        cases,digest = load_suite(body.suite_version)
    except ValueError:
        raise EvaluationError('INVALID_SUITE','未知或无效评测集',422) from None
    if runner._running:
        raise EvaluationError('EVALUATION_RUNNING','已有评测正在运行',409)
    eval_id = 'eval_' + uuid4().hex
    flags = body.feature_flags.model_dump()
    try:
        repository.create(eval_id,len(cases),metadata=archive.provenance(digest,flags))
    except Exception:
        raise EvaluationError('EVALUATION_STORAGE_FAILED','评测归档不可用') from None
    runner._running = True
    loop = asyncio.get_running_loop()
    loop._smartbrief_evaluation_id = eval_id
    loop._smartbrief_evaluation_handle = asyncio.create_task(_execute(eval_id,cases,flags))
    return dict(eval_id=eval_id,status='running',total_cases=len(cases),mode='offline')


async def _execute(eval_id,cases,flags):
    try:
        await archive.execute_suite(eval_id,cases,flags)
    except asyncio.CancelledError:
        repository.fail(eval_id,'服务关闭：评测中断，已完成部分保留')
    except Exception:
        # archive已保存可追溯失败；不将内部异常原文暴露或再造成假完成。
        pass
    finally:
        runner._running = False


async def close():
    handle = getattr(asyncio.get_running_loop(),'_smartbrief_evaluation_handle',None)
    if handle is not None and not handle.done():
        handle.cancel()
        await asyncio.gather(handle,return_exceptions=True)
        # 协程尚未开始就取消时，不会进入_execute的finally，仍需归位。
        eval_id = asyncio.get_running_loop()._smartbrief_evaluation_id
        try:
            if repository.get(eval_id)['status'] == 'running':
                repository.fail(eval_id,'服务关闭：评测中断，已完成部分保留')
        finally:
            runner._running = False


def get(eval_id):
    try:
        record = repository.get(eval_id)
        if record is not None and (not isinstance(record['results'],list) or not isinstance(record['metrics'],dict) or record['metadata'] is not None and not isinstance(record['metadata'],dict)):
            raise ValueError('归档结构损坏')
        return record
    except Exception:
        raise EvaluationError('EVALUATION_STORAGE_FAILED','评测归档不可用') from None


def list_reports(page,page_size):
    try:
        items,total = repository.list_reports(page_size,(page-1)*page_size)
        return dict(items=items,total=total,page=page,page_size=page_size)
    except Exception:
        raise EvaluationError('EVALUATION_STORAGE_FAILED','评测归档不可用') from None


def compare(current_id,baseline_id):
    current,baseline = get(current_id),get(baseline_id)
    if current is None or baseline is None:
        raise EvaluationError('EVALUATION_NOT_FOUND','评测不存在',404)
    a,b = current['metadata'],baseline['metadata']
    keys = ('mode','suite_version','dataset_hash','model','prompt_version','code_commit','source_hash','randomness','memory_protocol')
    if current['status'] != 'completed' or baseline['status'] != 'completed' or not a or not b or any(a.get(k) is None or a.get(k) != b.get(k) for k in keys):
        raise EvaluationError('INCOMPARABLE_EVALUATIONS','需完整且同数据、模型、提示、源码与记忆协议的评测',409)
    changed = [key for key in ('rag','memory','review') if a['feature_flags'][key] != b['feature_flags'][key]]
    if len(changed) != 1:
        raise EvaluationError('INCOMPARABLE_EVALUATIONS','对照必须且只能改变一个开关',409)
    am,bm = current['metrics']['quality'],baseline['metrics']['quality']
    return dict(current_id=current_id,baseline_id=baseline_id,changed_feature=changed[0],mode=a['mode'],
        quality_pass_rate_delta=am['quality_pass_rate']-bm['quality_pass_rate'],
        execution_completion_rate_delta=am['execution_completion_rate']-bm['execution_completion_rate'],
        interpretation='合成协议差值；有限样本与固定响应不能证明在线质量提升，人工事实抽查仍pending')
