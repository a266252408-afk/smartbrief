"""评测不得把拒绝/未知/失败放行纳入质量成功。"""
import asyncio
import pytest
from db import database, task_repository as repo
from evaluation import runner, metrics


@pytest.mark.parametrize('quality,forced,expected', [('passed',False,True),('rejected',True,False),('unavailable',False,False),('unknown',False,False),('passed',True,False)])
def test_case_quality_success(tmp_path, monkeypatch, quality, forced, expected):
    database.init_db(tmp_path/'eval.db')
    async def workflow(task_id, topic):
        repo.update_task(task_id,status='completed',quality_status=quality,forced_output=forced,final_report='报告')
    monkeypatch.setattr(runner.workflow,'run_task',workflow)
    result=asyncio.run(runner._run_single_case({'topic':'主题','keywords':['报告']},1))
    assert result['execution_success'] is True
    assert result['quality_passed'] is expected
    assert result['success'] is expected
    assert result['quality_status'] == quality


def test_metric_denominators_do_not_confuse_quality_with_completion():
    result=metrics.compute_metrics([
        {'success':True,'execution_success':True,'quality_passed':True,'duration_seconds':2},
        {'success':True,'execution_success':True,'quality_passed':False,'duration_seconds':4},
        {'success':True,'execution_success':False,'quality_passed':False},
        {'success':True},
    ])['summary']
    assert result['success_rate'] == .25
    assert result['execution_success_rate'] == .5
    assert result['metric_version'] == 2


@pytest.mark.parametrize('review,expected', [('{"passed":true,"comments":[]}',True),('bad json',False),('{"passed":false,"comments":["证据不足"]}',False)])
def test_runner_through_real_graph_and_repository(tmp_path, monkeypatch, offline_evidence, review, expected):
    from agents import planner, react_writer, reviewer, rewriter
    from tests.fakes import ScriptedLLM
    database.init_db(tmp_path/'real-eval.db')
    plan='[{"id":1,"agent":"researcher","task":"搜资料","depends_on":[]},{"id":2,"agent":"writer","task":"报告","depends_on":[1]}]'
    for module, outputs in [(planner,[plan]),(react_writer,['## 报告[S1]']), (reviewer,[review]*3),(rewriter,['## 修改报告[S1]']*2)]:
        monkeypatch.setattr(module,'chat',ScriptedLLM(outputs).chat)
    result=asyncio.run(runner._run_single_case({'topic':'主题','keywords':['报告']},1))
    assert result['execution_success'] is True
    assert result['quality_passed'] is expected
    assert result['success'] is expected
    assert repo.get_task(result['task_id'])['quality_status'] == result['quality_status']
