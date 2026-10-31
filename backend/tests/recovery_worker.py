"""仅由恢复演练测试启动的子进程；外部依赖替身，真实图/SQLite保持。"""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


async def execute(directory,mode,window):
    # 必须先禁dotenv和安装离线守卫，再导入任何应用配置。
    os.environ['PYTHON_DOTENV_DISABLED']='1'
    os.environ['DATA_DIR']=str(directory/'runtime')
    from tests.offline_guard import install
    install()
    from agents import planner, react_researcher, react_writer, reviewer, rewriter
    from db import database, task_repository
    from db.model_route_repository import bind_backup, get_route
    from graph import workflow
    from memory import checkpointer, long_term_memory
    from rag import store
    from services.model_routing import ModelRoute
    from services.task_service import TaskService
    from tests.fakes import ScriptedLLM, fixture_search
    database.DB_PATH=directory/'tasks.db'
    checkpointer.CHECKPOINT_DB=directory/'checkpoints.db'
    database.init_db()
    tid='process-units'

    def write(name,data):
        path=directory/name
        with path.open('w',encoding='utf-8') as file:
            json.dump(data,file,ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())

    def trace(kind,**data):
        with (directory/'trace.jsonl').open('a',encoding='utf-8') as file:
            file.write(json.dumps(dict(kind=kind,recorded_at=datetime.now(timezone.utc).isoformat(),**data),ensure_ascii=False)+'\n')
            file.flush()
            os.fsync(file.fileno())

    def event(label):
        with (directory/'events.txt').open('a',encoding='utf-8') as file:
            file.write(label+'\n')
            file.flush()
            os.fsync(file.fileno())
        trace('agent_call',label=label)

    async def barrier():
        async with checkpointer.open_checkpointer() as (cp,kind):
            assert kind=='sqlite'
            snapshot=await workflow.build_graph(checkpointer=cp).aget_state({'configurable':{'thread_id':tid}})
            assert snapshot.values['completed_steps']==[1,2]
            trace('checkpoint_before_interrupt',committed=snapshot.values['completed_steps'],interrupted='writer:B',window=window)
            write('barrier.json',dict(committed=snapshot.values['completed_steps'],interrupted='writer:B',window=window))
        await asyncio.Event().wait()

    plan=[dict(id=1,agent='researcher',task='研究A',depends_on=[]),
          dict(id=2,agent='writer',task='章节A',depends_on=[1]),
          dict(id=3,agent='writer',task='章节B',depends_on=[2])]
    scripts={planner:ScriptedLLM([json.dumps(plan)]),
             react_researcher:ScriptedLLM(['{"action":"web_search","query":"合成资料"}','{"action":"done"}']),
             reviewer:ScriptedLLM(['{"passed":true,"comments":[]}']),rewriter:ScriptedLLM([])}
    real_step=workflow.scheduler_step_node
    async def step(state):
        result=await real_step(state)
        if mode=='first' and window=='before_commit' and 3 in result.get('completed_steps',[]):
            await barrier()
        return result

    def adapter(module,script):
        async def call(messages,**kwargs):
            event('researcher' if module is react_researcher else module.__name__.split('.')[-1])
            return await script.chat(messages,**kwargs)
        return call

    async def writer(messages,**kwargs):
        label='writer:B' if '章节B' in str(messages) else 'writer:A'
        event(label)
        if mode=='first' and label=='writer:B' and window=='executing':
            await barrier()
        from types import SimpleNamespace
        text='## 章节B\n\n离线资料说明。[S1]' if label=='writer:B' else '## 章节A\n\n离线资料说明。[S1]'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text,tool_calls=None))])

    with ExitStack() as stack:
        for module,script in scripts.items():
            stack.enter_context(patch.object(module,'chat',adapter(module,script)))
        stack.enter_context(patch.object(react_writer,'chat',writer))
        stack.enter_context(patch.object(workflow,'scheduler_step_node',step))
        stack.enter_context(patch.object(react_researcher.mcp_manager,'call_tool',AsyncMock(return_value=fixture_search('合成资料'))))
        stack.enter_context(patch.object(long_term_memory,'_MEMORY_ENABLED',False))
        for name in ('search_template_structure','search_rag_terms'):
            stack.enter_context(patch.object(store,name,return_value=[]))
        if mode=='first':
            task_repository.create_task(tid,'进程恢复演练')
            bind_backup(ModelRoute(task_id=tid,model='fixture-backup',provider='fixture',reason='MODEL_CONNECTION',switched_at='fixture'))
            await workflow.run_task(tid,'进程恢复演练')
        else:
            task_repository.mark_interrupted_tasks_as_paused()
            service=TaskService()
            try:
                accepted=await service.resume(tid,key='process-resume')
                task=await service.wait(tid)
                write('result.json',dict(status=task['status'],resume_mode=accepted['resume_mode'],
                     completed_steps=task['completed_steps'],section_a_count=task['final_report'].count('章节A'),
                     evidence_count=len(task['evidence']),route=get_route(tid)))
                trace('recovery_result',status=task['status'],resume_mode=accepted['resume_mode'],completed_steps=task['completed_steps'])
            finally:
                await service.close()


if __name__=='__main__':
    asyncio.run(execute(Path(sys.argv[1]).resolve(),sys.argv[2],sys.argv[3]))
