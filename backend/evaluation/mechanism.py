"""固定白名单机制回归适配器；执行真实测试并解析JUnit，不执行题库中的代码。"""
import asyncio
import json
import os
from pathlib import Path
import sys
import signal
import subprocess
import sqlite3
from time import perf_counter
from typing import Literal
import xml.etree.ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field, model_validator

REPO=Path(__file__).resolve().parents[2]
TESTS = {
    'G023':('test_agent_checkpoints.py','test_resume_preserves_completed_units'),
    'R009':('test_model_failover.py','test_switch_stays_on_backup_after_recovery_and_reopen'),
    'R010':('test_model_failover.py','test_primary_auth_failure_can_switch'),
    'R011':('test_model_failover.py','test_control_and_request_errors_never_switch'),
    'R012':('test_model_failover.py','test_budget_exhausted_by_failed_primary_blocks_backup'),
    'R013':('test_model_failover.py','test_usage_persistence_failure_prevents_failover'),
    'R014':('test_model_failover.py','test_binding_failure_never_calls_backup'),
    'R015':('test_model_failover.py','test_both_unavailable_pause_workflow_without_losing_checkpoint'),
    'R016':('test_model_failover.py','test_service_resumes_model_pause_on_backup_only'),
    'R017':('test_model_failover.py','test_next_task_probes_primary_after_cooldown'),
    'R018':('test_model_failover.py','test_concurrent_new_tasks_have_one_recovery_probe'),
    'R019':('test_agent_checkpoints.py','test_resume_preserves_completed_units'),
    'R020':('test_task_recovery.py','test_resume_repeated_concurrent_requests_create_one_execution'),
    'R021':('test_task_recovery.py','test_resume_missing_checkpoint_is_explicit_restart'),
    'R022':('test_task_recovery.py','test_resume_does_not_reset_usage_budget'),
    'R023':('test_cache_usage.py','test_cache_price_does_not_double_count_input_and_is_historical'),
    'C026':('test_evaluation_process_recovery.py','test_real_process_checkpoint_resume[executing]'),
    'C027':('test_evaluation_process_recovery.py','test_real_process_checkpoint_resume[before_commit]'),
}


class MechanismRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    profile: Literal['mechanism_regression']='mechanism_regression'
    case_ids: list[str]=Field(min_length=1,max_length=20)

    @model_validator(mode='after')
    def check_cases(self):
        if len(self.case_ids)!=len(set(self.case_ids)) or any(cid not in TESTS for cid in self.case_ids):
            raise ValueError('案例重复或尚无机制回归适配器')
        return self


class MechanismResult(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    case_id: str
    check_status: Literal['pass','fail','unknown']
    test_count: int
    passed: int
    failed: int
    skipped: int
    test_names: list[str]
    execution_status: Literal['completed','failed']
    report_quality: Literal['unknown']='unknown'
    exit_code: int | None
    duration_seconds: float
    engine: Literal['pytest_mechanism_regression']='pytest_mechanism_regression'
    error: str | None
    scope: str
    trace: list[dict] = []


class MechanismSummary(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    planned: int
    processed: int
    passed: int
    failed: int
    unknown: int


class MechanismRun(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    run_id: str
    profile: Literal['mechanism_regression']
    execution_status: Literal['running','completed','failed']
    case_ids: list[str]
    results: list[MechanismResult]
    metadata: dict
    created_at: str
    updated_at: str
    error: str | None
    report_quality: Literal['unknown']
    summary: MechanismSummary


def parse_junit(path,exit_code):
    counts=dict(test_count=0,passed=0,failed=0,skipped=0)
    names=[]
    try:
        for case in ET.parse(path).getroot().iter('testcase'):
            counts['test_count']+=1
            names.append(case.attrib.get('name','未命名'))
            if case.find('failure') is not None or case.find('error') is not None:
                counts['failed']+=1
            elif case.find('skipped') is not None:
                counts['skipped']+=1
            else:
                counts['passed']+=1
    except (OSError,ET.ParseError):
        pass
    status='unknown'
    if counts['failed'] and exit_code in (0,1):
        status='fail'
    elif counts['test_count'] and counts['passed']==counts['test_count'] and exit_code==0:
        status='pass'
    return dict(**counts,check_status=status,test_names=names)


def child_environment(directory):
    env={key:value for key,value in os.environ.items()
         if not any(secret in key.upper() for secret in ('API_KEY','TOKEN','PASSWORD','SECRET'))
         and not key.startswith(('COV_CORE_','COVERAGE_','PYTEST_'))}
    env.update(PYTHON_DOTENV_DISABLED='1',PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1',
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',DATA_DIR=str(directory/'runtime'),
               TEMP=str(directory),TMP=str(directory),MODEL_FAILOVER_ENABLED='false',
               PYTHONPATH=os.pathsep.join([str(REPO/'backend'),str(REPO)]))
    return env


async def stop_owned_process(process):
    """仅结束本函数启动的评测进程树；中断演练可能包含嵌套子进程。"""
    if process.returncode is not None:
        return
    if sys.platform=='win32':
        try:
            result=await asyncio.to_thread(subprocess.run,['taskkill','/PID',str(process.pid),'/T','/F'],
                                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                    timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode and process.returncode is None:
                process.kill()
        except (OSError,subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


def read_trace(directory):
    """只提取本次隔离目录的白名单字段；不公开原始日志或提示。"""
    events=[]
    for path in sorted((directory/'pytest').rglob('*.db')):
        try:
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as conn:
                for row in conn.execute('SELECT data FROM model_usage ORDER BY rowid LIMIT 100'):
                    data=json.loads(row[0])
                    events.append(dict(kind='model_call',**{key:data.get(key) for key in
                        ('model_role','status','error_code','duration_ms','logical_call_id','created_at')}))
        except (sqlite3.Error,ValueError,TypeError):
            continue
    for path in sorted((directory/'pytest').rglob('trace.jsonl')):
        for line in path.read_text('utf-8').splitlines()[:50]:
            data=json.loads(line)
            events.append({key:data[key] for key in ('kind','label','recorded_at','committed','interrupted',
                          'window','status','resume_mode','completed_steps') if key in data})
    return sorted(events,key=lambda e:e.get('recorded_at') or e.get('created_at') or '')


async def run_case(case_id,directory,timeout=120):
    if case_id not in TESTS:
        raise ValueError('案例未接通')
    directory=Path(directory).resolve()
    directory.mkdir(parents=True,exist_ok=True)
    filename,function=TESTS[case_id]
    junit=directory/'junit.xml'
    command=[sys.executable,'-m','pytest',f'tests/{filename}::{function}', '-q','-p','no:cacheprovider',
             '-o','addopts=',f'--basetemp={directory/"pytest"}',f'--junitxml={junit}']
    start=perf_counter()
    exit_code=None
    process=None
    reason=None
    try:
        with (directory/'output.txt').open('wb') as output:
            options=({'creationflags':subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                     if sys.platform=='win32' else {'start_new_session':True})
            process=await asyncio.create_subprocess_exec(*command,cwd=REPO/'backend',env=child_environment(directory),
                                                         **options,
                                                         stdout=output,stderr=asyncio.subprocess.STDOUT)
            exit_code=await asyncio.wait_for(process.wait(),timeout)
    except TimeoutError:
        reason='评测子进程超时，未获得通过凭据'
    except OSError:
        reason='评测子进程启动失败，请检查本地测试环境'
    finally:
        if process and process.returncode is None:
            await stop_owned_process(process)
    result=parse_junit(junit,exit_code)
    return MechanismResult(case_id=case_id,**result,execution_status='completed' if exit_code in (0,1) else 'failed',
                report_quality='unknown',exit_code=exit_code,duration_seconds=round(perf_counter()-start,3),
                engine='pytest_mechanism_regression',error=reason,
                scope='真实路由/恢复保护测试；外部供应商为替身，不证明在线可用性或报告质量',
                trace=read_trace(directory)).model_dump()
