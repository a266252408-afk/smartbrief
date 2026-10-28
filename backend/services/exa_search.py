"""EXA 搜索服务：调用 EXA 官方 Search API 并归一化结果。

与 `mcp_servers/web_search_server.py` 返回结构保持一致，便于技能与
research 智能体复用：
    {"source": "exa", "query": ..., "results": [{"title", "snippet", "url"}], ...}

配置：
    EXA_API_KEY  通过 `backend/.env` 或进程环境变量提供；未配置时返回明确错误，
                 不发起网络请求，也不使用虚构资料填充结果。

接口：
    POST https://api.exa.ai/search
    Header: x-api-key: <EXA_API_KEY>
    Body:   {"query": ..., "numResults": n, "contents": {"highlights": true}}
"""
from datetime import datetime, timezone
import json
import os
import re
import urllib.error
import urllib.request

EXA_ENDPOINT = "https://api.exa.ai/search"
REQUEST_TIMEOUT_SECONDS = 20
MAX_RESULTS = 10
SNIPPET_MAX_CHARS = 500


def is_configured() -> bool:
    """是否已配置 EXA_API_KEY（不校验 Key 是否真实可用）。"""
    return bool((os.getenv("EXA_API_KEY") or "").strip())


def _api_key() -> str:
    return (os.getenv("EXA_API_KEY") or "").strip()


def _post_json(payload: dict, key: str, timeout: float) -> dict:
    """向 EXA 发送 JSON 请求并解析响应；异常交由调用方统一降级。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        EXA_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": key,
            "User-Agent": "SmartBrief-EXA-Search/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8", errors="ignore"))
    if not isinstance(data, dict):
        raise ValueError("EXA 响应不是 JSON 对象")
    return data


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    """提取 EXA 错误响应中的可读信息（不包含 API Key）。"""
    detail = ""
    try:
        data = json.loads(exc.read().decode("utf-8", errors="ignore"))
        if isinstance(data, dict):
            detail = str(data.get("error") or data.get("message") or "").strip()
    except (OSError, ValueError):
        pass
    status = getattr(exc, "code", None)
    prefix = f"EXA 接口返回 HTTP {status}" if status else "EXA 接口请求失败"
    return f"{prefix}：{detail}" if detail else prefix


def _snippet(item: dict) -> str:
    """从 highlights / summary / text 中取摘要并压缩空白。"""
    text = ""
    highlights = item.get("highlights")
    if isinstance(highlights, list):
        text = " ".join(str(part) for part in highlights if part)
    if not text:
        text = str(item.get("summary") or item.get("text") or "")
    return re.sub(r"\s+", " ", text).strip()[:SNIPPET_MAX_CHARS]


def exa_search(
    query: str,
    num_results: int = 5,
    *,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict:
    """调用 EXA 搜索并返回统一结构；失败时 results 为空并附带 error。"""
    query = (query or "").strip()
    result: dict = {"source": "exa", "query": query, "results": []}
    if not query:
        result.update(error="搜索关键词为空", error_code="SEARCH_INVALID_QUERY")
        return result

    try:
        count = int(num_results)
    except (TypeError, ValueError):
        count = 5
    count = min(max(count, 1), MAX_RESULTS)

    key = _api_key()
    if not key:
        result.update(error="未配置 EXA_API_KEY，无法调用 EXA 搜索", error_code="SEARCH_UNAVAILABLE")
        return result

    payload = {"query": query, "numResults": count, "contents": {"highlights": True}}
    try:
        data = _post_json(payload, key, timeout)
    except urllib.error.HTTPError as exc:
        result.update(error=_http_error_message(exc), error_code="SEARCH_UNAVAILABLE")
        return result
    except Exception as exc:  # 网络、超时、JSON 解析等统一降级
        result.update(
            error=f"EXA 搜索失败：{type(exc).__name__}: {exc}",
            error_code="SEARCH_UNAVAILABLE",
        )
        return result

    items = data.get("results")
    if not isinstance(items, list):
        result.update(error="EXA 返回格式异常：缺少 results 列表", error_code="SEARCH_UNAVAILABLE")
        return result

    for item in items:
        if len(result["results"]) >= count:
            break
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        title = str(item.get("title") or "").strip() or url
        result["results"].append({"title": title, "snippet": _snippet(item), "url": url})

    result["search_endpoint"] = EXA_ENDPOINT
    result["retrieved_at"] = datetime.now(timezone.utc).isoformat()
    # 空结果如实返回，不补造资料。
    return result
