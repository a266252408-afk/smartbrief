"""固定白名单机制回归适配器；执行真实测试并解析JUnit，不执行题库中的代码。"""
import asyncio
import os
from pathlib import Path
import sys
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
            process=await asyncio.create_subprocess_exec(*command,cwd=REPO/'backend',env=child_environment(directory),
                                                         stdout=output,stderr=asyncio.subprocess.STDOUT)
            exit_code=await asyncio.wait_for(process.wait(),timeout)
    except TimeoutError:
        reason='评测子进程超时，未获得通过凭据'
    except OSError:
        reason='评测子进程启动失败，请检查本地测试环境'
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
    result=parse_junit(junit,exit_code)
    return MechanismResult(case_id=case_id,**result,execution_status='completed' if exit_code in (0,1) else 'failed',
                report_quality='unknown',exit_code=exit_code,duration_seconds=round(perf_counter()-start,3),
                engine='pytest_mechanism_regression',error=reason,
                scope='真实路由/恢复保护测试；外部供应商为替身，不证明在线可用性或报告质量').model_dump()
