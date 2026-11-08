"""EXA 搜索服务测试（services/exa_search.py）。

全部使用 HTTP 替身，不访问外部网络、不消耗真实 Key。
"""
import io
import json
import urllib.error

import pytest

from services import exa_search as exa


def response(**overrides):
    data = {
        "requestId": "req-1",
        "results": [
            {
                "title": "示例标题",
                "url": "https://example.invalid/a",
                "highlights": ["相关摘要 A", "补充摘要"],
            }
        ],
    }
    data.update(overrides)
    return data


def patch_post(monkeypatch, fake):
    monkeypatch.setattr(exa, "_post_json", fake)


# ---------------------------------------------------------------- 请求构造


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def test_post_json_uses_post_and_api_key_header(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(b'{"results": []}')

    monkeypatch.setattr(exa.urllib.request, "urlopen", fake_urlopen)
    data = exa._post_json({"query": "测试"}, "secret-key", 3)
    assert data == {"results": []}
    assert captured["request"].get_method() == "POST"
    assert captured["request"].full_url == exa.EXA_ENDPOINT
    headers = {key.lower(): value for key, value in captured["request"].header_items()}
    assert headers["x-api-key"] == "secret-key"
    assert headers["content-type"] == "application/json"
    assert json.loads(captured["request"].data) == {"query": "测试"}
    assert captured["timeout"] == 3


# ---------------------------------------------------------------- 成功路径


def test_search_success_maps_fields_and_payload(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    captured = {}

    def fake_post(payload, key, timeout):
        captured.update(payload=payload, key=key, timeout=timeout)
        return response()

    patch_post(monkeypatch, fake_post)
    result = exa.exa_search("测试查询", 3)

    assert result["source"] == "exa"
    assert result["query"] == "测试查询"
    assert result["results"] == [
        {
            "title": "示例标题",
            "snippet": "相关摘要 A 补充摘要",
            "url": "https://example.invalid/a",
        }
    ]
    assert result["search_endpoint"] == exa.EXA_ENDPOINT
    assert result["retrieved_at"]
    assert "error" not in result
    assert captured["key"] == "test-key"
    assert captured["payload"] == {
        "query": "测试查询",
        "numResults": 3,
        "contents": {"highlights": True},
    }
    assert captured["timeout"] == exa.REQUEST_TIMEOUT_SECONDS


def test_text_used_when_highlights_missing(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    patch_post(
        monkeypatch,
        lambda payload, key, timeout: response(
            results=[
                {"title": "标题", "url": "https://example.invalid/b", "text": "整页正文\n  中的一段"}
            ]
        ),
    )
    assert exa.exa_search("q", 1)["results"][0]["snippet"] == "整页正文 中的一段"


def test_snippet_preserves_unsplit_unit_within_storage_limit(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    patch_post(
        monkeypatch,
        lambda payload, key, timeout: response(
            results=[{"title": "长文", "url": "https://example.invalid/c", "text": "字" * 800}]
        ),
    )
    result = exa.exa_search("q", 1)
    assert result["results"][0]["snippet"] == "字" * 800


def test_invalid_items_skipped_and_title_falls_back_to_url(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    patch_post(
        monkeypatch,
        lambda payload, key, timeout: response(
            results=[
                "not-a-dict",
                {"title": "无链接"},
                {"title": "", "url": "https://example.invalid/d"},
            ]
        ),
    )
    result = exa.exa_search("q", 5)
    assert result["results"] == [
        {"title": "https://example.invalid/d", "snippet": "", "url": "https://example.invalid/d"}
    ]


@pytest.mark.parametrize(
    "requested,expected",
    [(99, exa.MAX_RESULTS), (0, 1), (-3, 1), ("x", 5), (None, 5)],
)
def test_num_results_clamped(monkeypatch, requested, expected):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    captured = {}

    def fake_post(payload, key, timeout):
        captured["numResults"] = payload["numResults"]
        return response()

    patch_post(monkeypatch, fake_post)
    exa.exa_search("q", requested)
    assert captured["numResults"] == expected


def test_is_configured(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "key")
    assert exa.is_configured() is True
    monkeypatch.setenv("EXA_API_KEY", "  ")
    assert exa.is_configured() is False


# ---------------------------------------------------------------- 失败与空结果


def test_missing_api_key_returns_error(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "")
    called = []
    patch_post(monkeypatch, lambda *args: called.append(args))
    result = exa.exa_search("q", 3)
    assert result["results"] == []
    assert result["error_code"] == "SEARCH_UNAVAILABLE"
    assert "EXA_API_KEY" in result["error"]
    assert called == []


def test_empty_query_returns_error(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    result = exa.exa_search("  ", 3)
    assert result["results"] == []
    assert result["error_code"] == "SEARCH_INVALID_QUERY"


def test_empty_results_are_not_fabricated(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    patch_post(monkeypatch, lambda payload, key, timeout: {"requestId": "r", "results": []})
    result = exa.exa_search("nothing", 3)
    assert result["results"] == []
    assert "error" not in result


def test_missing_results_list_returns_format_error(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    patch_post(monkeypatch, lambda payload, key, timeout: {"requestId": "r"})
    result = exa.exa_search("q", 3)
    assert result["error_code"] == "SEARCH_UNAVAILABLE"
    assert "results" in result["error"]


def test_http_error_reports_status_and_detail(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "super-secret")

    def fake_post(payload, key, timeout):
        raise urllib.error.HTTPError(
            exa.EXA_ENDPOINT,
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error": "Invalid API key", "tag": "AUTH"}'),
        )

    patch_post(monkeypatch, fake_post)
    result = exa.exa_search("q", 3)
    assert result["results"] == []
    assert result["error_code"] == "SEARCH_UNAVAILABLE"
    assert "HTTP 401" in result["error"]
    assert "Invalid API key" in result["error"]
    assert "super-secret" not in result["error"]


def test_network_error_returns_unavailable(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    def fake_post(payload, key, timeout):
        raise OSError("network down")

    patch_post(monkeypatch, fake_post)
    result = exa.exa_search("q", 3)
    assert result["results"] == []
    assert result["error_code"] == "SEARCH_UNAVAILABLE"
    assert "network down" in result["error"]
