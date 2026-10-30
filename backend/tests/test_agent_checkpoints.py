"""真实SQLite与任务恢复入口：已完成Agent执行单元不得重新调用。"""
import asyncio
import json

import pytest

from db import database, task_repository
from graph import workflow
from memory import checkpointer
from services.task_service import TaskService
from services.usage_service import UsagePersistenceError


@pytest.mark.parametrize('interrupted_agent', ['writer', 'react_researcher'])
def test_resume_preserves_completed_units(offline_evidence, interrupted_agent):
    database.init_db()
    scripts = offline_evidence
    steps = [
        dict(id=1, agent='researcher', task='研究A', depends_on=[]),
        dict(id=2, agent='writer', task='章节A', depends_on=[1]),
        dict(id=3, agent='writer' if interrupted_agent == 'writer' else 'researcher',
             task='中断单元', depends_on=[2]),
    ]
    scripts['planner'].outputs = [json.dumps(steps)]
    scripts['react_writer'].outputs = ['## 章节A[S1]']
    if interrupted_agent == 'writer':
        scripts['react_writer'].outputs += [UsagePersistenceError('离线注入中断'), '## 章节B[S1]']
    else:
        scripts['react_researcher'].outputs += [UsagePersistenceError('离线注入中断'), '{"action":"done"}']
    scripts['reviewer'].outputs = ['{"passed":true,"comments":[]}']
    task_repository.create_task('units', '单元恢复')

    async def scenario():
        with pytest.raises(UsagePersistenceError):
            await workflow.run_task('units', '单元恢复')
        async with checkpointer.open_checkpointer() as (cp, kind):
            assert kind == 'sqlite'
            snapshot = await workflow.build_graph(checkpointer=cp).aget_state(
                {'configurable': {'thread_id': 'units'}})
            assert snapshot.next == ('scheduler',)
            assert snapshot.values['completed_steps'] == [1, 2]
            assert '章节A' in snapshot.values['draft_content']
            assert snapshot.values['evidence']
        before = len(scripts['react_researcher'].calls)
        service = TaskService()
        accepted = await service.resume('units', key='unit-resume')
        assert accepted['resume_mode'] == 'checkpoint'
        task = await service.wait('units')
        await service.close()
        assert task['status'] == 'completed'
        assert task['completed_steps'] == [1, 2, 3]
        assert task['final_report'].count('章节A') == 1
        assert len(scripts['planner'].calls) == 1
        if interrupted_agent == 'writer':
            assert len(scripts['react_researcher'].calls) == before
            assert len(scripts['react_writer'].calls) == 3
            assert '章节B' in task['final_report']
        else:
            assert len(scripts['react_researcher'].calls) == before + 1
            assert len(scripts['react_writer'].calls) == 1
        assert sum('步骤 2 完成' == log['content'] for log in task_repository.get_logs('units')) == 1

    asyncio.run(scenario())


def test_legacy_scheduler_snapshot_needs_no_new_section_field(offline_evidence):
    """旧Planner快照没有新增通道，仍可执行所有单元且不丢章节。"""
    from langgraph.graph import START, END, StateGraph
    from typing import TypedDict

    class OldState(TypedDict):
        task_id: str
        plan: list
        completed_steps: list
        research_data: dict
        draft_content: str
        react_logs: list

    initial = dict(task_id='legacy', plan=[dict(id=1, agent='writer', task='章节', depends_on=[])],
                   completed_steps=[], research_data={}, draft_content='', react_logs=[])

    async def scenario():
        async with checkpointer.open_checkpointer() as (cp, _):
            old = StateGraph(OldState)
            old.add_node('scheduler', lambda state: {})
            old.add_edge(START, 'scheduler')
            old.add_edge('scheduler', END)
            await old.compile(checkpointer=cp, interrupt_before=['scheduler']).ainvoke(
                initial, {'configurable': {'thread_id': 'legacy'}})
        async with checkpointer.open_checkpointer() as (cp, _):
            final = await workflow.build_graph(checkpointer=cp).ainvoke(
                None, {'configurable': {'thread_id': 'legacy'}})
            assert final['completed_steps'] == [1]
            assert '数据待补充' in final['draft_content']

    asyncio.run(scenario())
