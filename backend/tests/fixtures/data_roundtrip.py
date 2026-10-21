"""独立进程离线夹具：真实运行入口、SQLite、上传、Chroma和检查点。"""
import asyncio
import json
from importlib import import_module
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import_module('tests.offline_guard').install()
database = import_module('db.database')
task_repository = import_module('db.task_repository')
checkpointer = import_module('memory.checkpointer')
long_term_memory = import_module('memory.long_term_memory')
store = import_module('rag.store')
FakeEmbeddings = import_module('tests.fakes').FakeEmbeddings
approved_report = import_module('tests.memory_support').approved_report
builtin_tools = import_module('tools.builtin_tools')
mcp_client = import_module('mcp_client.mcp_client')
skills_manager = import_module('skills.skills_manager')
graph_module = import_module('langgraph.graph')
StateGraph, START, END = graph_module.StateGraph, graph_module.START, graph_module.END

store._embeddings = FakeEmbeddings()
database.init_db()
mode = sys.argv[1]
text = '仅用于持久化回归的固定资料'


class TinyState(TypedDict):
    value: int


async def checkpoint():
    graph = StateGraph(TinyState)
    graph.add_node('step', lambda state: {'value': state['value'] + 1})
    graph.add_edge(START, 'step')
    graph.add_edge('step', END)
    async with checkpointer.open_checkpointer() as (cp, kind):
        assert kind == 'sqlite'
        result = await graph.compile(checkpointer=cp).ainvoke({'value': 1}, {'configurable': {'thread_id': 'persist'}})
        assert result['value'] == 2


if mode == 'write':
    approved_report('持久化主题', '# 原报告[S1]', 'persist')
    assert long_term_memory.remember('持久化主题', '# 原报告[S1]', task_id='persist')
    upload = store.ingest_file(text.encode(), '资料.txt')
    builtin_tools.init()
    builtin_tools._save_config()
    mcp_client._save_config([])
    skills_manager.manager.init()
    skill = skills_manager.SKILLS_POOL_DIR / 'fixture'
    skill.mkdir(parents=True)
    (skill / 'skill.md').write_text('离线自定义技能', 'utf-8')
    asyncio.run(checkpoint())
    (database.DB_PATH.parent / 'expected.json').write_text(json.dumps(upload), 'utf-8')
else:
    assert task_repository.get_task('persist')['final_report'] == '# 原报告[S1]'
    expected = json.loads((database.DB_PATH.parent / 'expected.json').read_text('utf-8'))
    assert store.get_document_by_id(expected['document_id'])['chunks'][0]['content'] == text
    assert store.search_rag(text)[0]['score'] == 1
    assert long_term_memory.recall('持久化主题')[0]['task_id'] == 'persist'
    assert checkpointer.has_checkpoint('persist')
    assert len(checkpointer.list_snapshots('persist')) >= 2
    assert builtin_tools._load_config()['builtin']
    assert mcp_client._load_config() == {'servers': []}
    assert skills_manager._load_skill_config()['skills']
    assert (skills_manager.SKILLS_POOL_DIR / 'fixture/skill.md').read_text('utf-8') == '离线自定义技能'
print('DATA_ROUNDTRIP_' + mode.upper() + '_PASSED')
