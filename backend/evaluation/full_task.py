"""完整任务契约和隔离执行器。"""
from typing import Literal
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from pydantic import BaseModel, ConfigDict, Field, model_validator
from evaluation.mechanism import REPO, child_environment, stop_owned_process

SUPPORTED = ['G017', 'G018', 'G019', 'G020']


class FullTaskRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    profile: Literal['full_task_local', 'full_task_live'] = 'full_task_local'
    case_ids: list[str] = Field(min_length=1, max_length=4)
    group: Literal['primary', 'backup', 'mixed'] = 'primary'
    authorization_id: str | None = None

    @model_validator(mode='after')
    def cases(self):
        if len(set(self.case_ids)) != len(self.case_ids) or not set(self.case_ids) <= set(SUPPORTED):
            raise ValueError('完整任务仅支持固定资料案例')
        if self.profile == 'full_task_local' and self.authorization_id is not None:
            raise ValueError('本地替身运行不接受付费授权')
        return self


class FullTaskResult(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    case_id: str
    check_status: Literal['pass', 'fail', 'unknown']
    task_execution_status: str
    business_quality: str
    report_quality: Literal['unknown'] = 'unknown'
    report: str
    report_sha256: str
    evidence: list[dict]
    trace: list[dict]
    calls: int | None
    input_tokens: int | None
    output_tokens: int | None
    duration_seconds: float
    error: str | None


async def run_case(case_id, group, directory, grant=None, timeout=180):
    if case_id not in SUPPORTED or group not in ('primary', 'backup', 'mixed'):
        raise ValueError('未接通的完整任务配置')
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    env = child_environment(directory)
    # 即使父进程配置了授权，本地子进程也不能继承它。
    env.pop('SMARTBRIEF_EVALUATION_GRANT', None)
    if grant is not None:
        env.update(SMARTBRIEF_EVALUATION_GRANT=grant.model_dump_json(), OPENAI_API_KEY=os.environ['OPENAI_API_KEY'])
    command = [sys.executable, '-m', 'evaluation.full_task_worker', str(directory), case_id, group]
    process = None
    start = perf_counter()
    reason = None
    try:
        with (directory / 'worker-output.txt').open('wb') as output:
            options = ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                       if sys.platform == 'win32' else {'start_new_session': True})
            process = await asyncio.create_subprocess_exec(*command, cwd=REPO / 'backend', env=env,
                stdout=output, stderr=asyncio.subprocess.STDOUT, **options)
            code = await asyncio.wait_for(process.wait(), timeout)
            if code != 0:
                raise ValueError('子进程异常')
            result = FullTaskResult.model_validate_json((directory / 'result.json').read_text('utf8'))
            if result.case_id != case_id:
                raise ValueError('结果归属错误')
            return result.model_dump()
    except (OSError, ValueError, TimeoutError):
        reason = '完整任务子进程启动、执行、解析或期限异常；未获得通过凭据'
    finally:
        if process is not None and process.returncode is None:
            await stop_owned_process(process)
    return FullTaskResult(case_id=case_id, check_status='unknown', task_execution_status='failed',
        business_quality='unknown', report='', report_sha256='', evidence=[], trace=[], calls=None,
        input_tokens=None, output_tokens=None, duration_seconds=round(perf_counter() - start, 3), error=reason).model_dump()
