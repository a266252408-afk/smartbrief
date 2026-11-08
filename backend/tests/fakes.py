"""确定性外部服务替身；所有来源均明确标记为测试夹具。"""
import hashlib
from types import SimpleNamespace


class ScriptedLLM:
    """按脚本消费响应/异常；耗尽即失败，防止隐式成功。"""
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if not self.outputs:
            raise AssertionError("LLM 响应脚本已耗尽")
        output = self.outputs.pop(0)
        if callable(output):
            output = output(messages)
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=output, tool_calls=None)
        )])


class FakeEmbeddings:
    """固定八维向量，保留真实 Chroma 入库和检索。"""
    def embed_query(self, text):
        return [value / 255.0 for value in hashlib.sha256(text.encode()).digest()[:8]]

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]


def fixture_search(query, num_results=5):
    if query == "故障":
        return {"query": query, "source": "fixture", "results": [], "error": "固定搜索故障"}
    items = [{"title": f"离线测试资料 {i}", "url": f"https://example.invalid/fixture/{i}",
              "snippet": "本条为测试夹具，不是实际研究证据。"} for i in range(2)]
    return {"query": query, "source": "fixture", "results": items[:max(0, num_results)]}
