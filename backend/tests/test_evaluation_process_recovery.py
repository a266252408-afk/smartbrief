"""独立测试进程退出后从真实检查点恢复；不结束用户服务。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


@pytest.mark.parametrize('window',['executing','before_commit'])
def test_real_process_checkpoint_resume(tmp_path,window):
    worker=Path(__file__).with_name('recovery_worker.py')
    env=dict(os.environ,PYTHON_DOTENV_DISABLED='1',PYTHONDONTWRITEBYTECODE='1',
             DATA_DIR=str(tmp_path/'runtime'),TEMP=str(tmp_path),TMP=str(tmp_path))
    with (tmp_path/'first-output.txt').open('wb') as log:
        child=subprocess.Popen([sys.executable,str(worker),str(tmp_path),'first',window],env=env,
                               cwd=Path(__file__).resolve().parents[1],stdout=log,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+30
            while time.monotonic()<deadline and child.poll() is None and not (tmp_path/'barrier.json').exists():
                time.sleep(.03)
            assert (tmp_path/'barrier.json').exists(),(tmp_path/'first-output.txt').read_text('utf-8',errors='replace')
            barrier=json.loads((tmp_path/'barrier.json').read_text('utf-8'))
            assert barrier['committed']==[1,2] and barrier['interrupted']=='writer:B'
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
    resumed=subprocess.run([sys.executable,str(worker),str(tmp_path),'resume',window],env=env,
                           cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=40)
    assert resumed.returncode==0,resumed.stdout.decode('utf-8',errors='replace')+resumed.stderr.decode('utf-8',errors='replace')
    result=json.loads((tmp_path/'result.json').read_text('utf-8'))
    assert result['status']=='completed' and result['resume_mode']=='checkpoint'
    assert result['completed_steps']==[1,2,3]
    assert result['section_a_count']==1 and result['evidence_count']>=1
    events=(tmp_path/'events.txt').read_text('utf-8').splitlines()
    assert events.count('planner')==1 and events.count('researcher')==2
    assert events.count('writer:A')==1 and events.count('writer:B')==2
    assert result['route']['role']=='backup'
