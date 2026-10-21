"""明确合成的供应商/搜索/嵌入替身；真实预算、图、状态与仓储不替换。"""
import asyncio
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import sqlite3

from evaluation.context import evaluation_context


class SyntheticEmbeddings:
    """固定向量用于确定性协议测试，不声称语义检索质量。"""
    def embed_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]


class OfflineDependencies:
    model = 'synthetic-protocol-1'

    def __init__(self, case, directory, flags=None):
        self.case = case
        self.directory = Path(directory)
        self.flags = flags or dict(rag=True, memory=True, review=True)
        self.calls = Counter()
        self.stores = {}
        self.entered_writer = asyncio.Event()

    def vector_store(self, collection):
        from rag.vectorstores import ChromaVectorStore
        if collection not in self.stores:
            self.stores[collection] = ChromaVectorStore(SyntheticEmbeddings(), str(self.directory / 'vectors'), collection)
        return self.stores[collection]

    def memory_connection(self):
        connection = sqlite3.connect(self.directory / 'memory.db', check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TABLE IF NOT EXISTS memory_entries(memory_id TEXT PRIMARY KEY,data TEXT NOT NULL)')
        return connection

    async def search(self, query):
        scenario = self.case['scenario']
        items = [] if scenario == 'no_sources' else [dict(title='合成协议资料A', url='https://example.invalid/protocol/a', snippet='合成样本：指标为12%，不是真实市场数据。')]
        if scenario == 'conflict':
            items.append(dict(title='合成协议资料B', url='https://example.invalid/protocol/b', snippet='合成冲突样本：同指标为24%，须保留分歧。'))
        if scenario == 'unknown_usage':
            items[0]['snippet'] += '第二资料缺失，未覆盖内容待补充。'
        return dict(query=query, source='synthetic-protocol', results=items)

    async def create(self, **kwargs):
        from services.usage_service import usage_context
        agent = usage_context.get()[1]
        self.calls[agent] += 1
        scenario = self.case['scenario']
        if agent == 'planner':
            text = json.dumps([dict(id=1, agent='researcher', task=self.case['topic'], depends_on=[]), dict(id=2, agent='writer', task=self.case['topic'], depends_on=[1])], ensure_ascii=False)
        elif agent == 'researcher':
            text = json.dumps(dict(action='web_search', query=self.case['topic']) if self.calls[agent] == 1 else dict(action='done'), ensure_ascii=False)
        elif agent in {'writer', 'rewriter'}:
            self.entered_writer.set()
            if scenario == 'cancel':
                await asyncio.Event().wait()
            if scenario == 'model_timeout':
                raise TimeoutError('固定离线供应商超时')
            text = '## 合成协议报告\n\n本报告仅验证软件协议，不代表事实准确性。\n\n'
            text += '数据待补充：没有可用来源。' if scenario == 'no_sources' else '合成样本指标12%。[S1]'
            if scenario == 'conflict':
                text += '\n\n另一合成资料指标24%，两者冲突，不能合并为事实。[S2]'
            if scenario == 'unknown_usage':
                text += '\n\n资料不完整：第二资料缺失，未覆盖内容数据待补充。'
        elif agent == 'reviewer':
            text = '无效审核返回' if scenario == 'review_invalid' else json.dumps(dict(passed=True, comments=[]))
        else:
            raise AssertionError('离线协议不支持此调用归属')
        usage = None if scenario == 'unknown_usage' else dict(prompt_tokens=100, completion_tokens=30)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))], usage=usage)

    def activate(self):
        return evaluation_context.set(self)


async def execute_case(dependencies):
    from graph.workflow import run_task
    from services.task_service import TaskService
    from rag.loaders import Document
    token = dependencies.activate()
    service = TaskService(runner=run_task)
    try:
        dependencies.directory.mkdir(parents=True, exist_ok=True)
        # 仅合成模板/术语进入独立Chroma，绝不读用户上传或真实向量目录。
        if dependencies.case['category'] != 'uncovered':
            dependencies.vector_store('rag_collection').add_documents([
                Document(page_content='## 合成协议结构\n## 资料与限制', metadata={'category':'template', 'source':'synthetic.md'}),
                Document(page_content='合成指标：仅供协议测试的固定值。', metadata={'category':'terminology', 'source':'synthetic.txt'})])
        result = await service.submit('[离线协议] ' + dependencies.case['topic'])
        task_id = result['task_id']
        if dependencies.case['scenario'] == 'cancel':
            await asyncio.wait_for(dependencies.entered_writer.wait(), 10)
            await service.cancel(task_id)
        await service.wait(task_id)
        return task_id
    finally:
        await service.close()
        evaluation_context.reset(token)
