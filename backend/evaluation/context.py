"""评测依赖上下文：随异步任务与线程传播，不改全局模型或知识库配置。"""
from contextvars import ContextVar

evaluation_context = ContextVar('evaluation_context', default=None)


def enabled(feature):
    context = evaluation_context.get()
    return context is None or context.flags[feature]
