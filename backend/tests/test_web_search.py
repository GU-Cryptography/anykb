"""web_search 的 You.com 首选来源 + DuckDuckGo 回退测试（v3-M7）。

纯单元：不触网、不需要真实 Key。You.com 路径用 httpx.MockTransport 打桩，
DuckDuckGo 路径用 monkeypatch 替换 _ddg_search，因此无需安装 ddgs。
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx

from src.tools import web_search as ws
from src.tools.web_search import WebSearchTool

UA = "youdotcom-integration/gu-cryptography-anykb"

SAMPLE = {
    "results": {
        "web": [
            {"url": "https://e.com/a", "title": "标题A", "description": "描述A",
             "snippets": ["片段A1", "片段A2"]},
            {"url": "https://e.com/b", "title": "标题B", "snippets": ["片段B1"]},
        ],
        "news": [
            {"url": "https://e.com/n", "title": "新闻N", "description": "新闻描述"},
        ],
    },
    "metadata": {"query": "t"},
}


def _set_key(monkeypatch, key: str) -> None:
    monkeypatch.setattr(ws, "get_settings", lambda: SimpleNamespace(ydc_api_key=key))


def _mock_youcom(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient  # capture the genuine class before patching
    def _factory(*_args, **kwargs):
        return real_client(
            transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout", 10)
        )
    monkeypatch.setattr(ws.httpx, "AsyncClient", _factory)


async def test_youcom_primary_maps_web_and_news(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=SAMPLE)

    _set_key(monkeypatch, "secret-key")
    _mock_youcom(monkeypatch, handler)

    res = await WebSearchTool().execute("测试", max_results=5)

    assert res.raw["source"] == "you.com"
    assert res.raw["count"] == 3
    assert "标题A" in res.text and "https://e.com/a" in res.text
    assert "新闻N" in res.text                 # news 也纳入
    assert "描述A" in res.text                 # description 作为摘要
    assert "片段B1" in res.text                # description 缺失回退 snippet
    assert captured["headers"]["X-API-Key"] == "secret-key"
    assert captured["headers"]["User-Agent"] == UA
    assert captured["params"]["query"] == "测试"
    assert "secret-key" not in res.text        # Key 绝不出现在返回内容


async def test_count_param_clamped_to_cap(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["count"] = request.url.params.get("count")
        return httpx.Response(200, json={"results": {"web": []}})

    _set_key(monkeypatch, "k")
    _mock_youcom(monkeypatch, handler)

    await WebSearchTool(max_results_default=3, max_results_cap=5).execute("q", max_results=99)
    assert captured["count"] == "5"


async def test_youcom_total_truncated_to_n(monkeypatch):
    body = {"results": {
        "web": [{"url": f"u{i}", "title": f"T{i}"} for i in range(4)],
        "news": [{"url": "un", "title": "N"}],
    }}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    _set_key(monkeypatch, "k")
    _mock_youcom(monkeypatch, handler)

    res = await WebSearchTool().execute("q", max_results=3)
    assert res.raw["count"] == 3               # web+news=5 → 按 n 截断为 3


async def test_youcom_error_falls_back_to_ddg(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        # 即便响应体里含有 Key，也绝不能泄露到返回内容
        return httpx.Response(500, text="server boom secret-key")

    _set_key(monkeypatch, "secret-key")
    _mock_youcom(monkeypatch, handler)
    monkeypatch.setattr(
        WebSearchTool, "_ddg_search",
        staticmethod(lambda q, n: [{"title": "DDG命中", "href": "https://d.uck/1", "body": "b"}]),
    )

    res = await WebSearchTool().execute("q")
    assert res.raw["source"] == "duckduckgo"
    assert "DDG命中" in res.text
    assert "secret-key" not in res.text


async def test_youcom_network_error_falls_back_to_ddg(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _set_key(monkeypatch, "k")
    _mock_youcom(monkeypatch, handler)
    monkeypatch.setattr(
        WebSearchTool, "_ddg_search",
        staticmethod(lambda q, n: [{"title": "DDG", "href": "https://d/1", "body": "b"}]),
    )

    res = await WebSearchTool().execute("q")
    assert res.raw["source"] == "duckduckgo"
    assert "DDG" in res.text


async def test_no_key_uses_ddg_and_never_calls_youcom(monkeypatch):
    _set_key(monkeypatch, "")

    def _boom(*_a, **_k):
        raise AssertionError("未配置 Key 时不得调用 You.com")

    monkeypatch.setattr(ws.httpx, "AsyncClient", _boom)
    monkeypatch.setattr(
        WebSearchTool, "_ddg_search",
        staticmethod(lambda q, n: [{"title": "DDG", "href": "https://d/1", "body": "b"}]),
    )

    res = await WebSearchTool().execute("q")
    assert res.raw["source"] == "duckduckgo"
    assert "DDG" in res.text


async def test_youcom_bad_json_falls_back_to_ddg(monkeypatch):
    # 200 但响应体非 JSON（或顶层非 dict）→ 解析异常 → 回退 DuckDuckGo
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json{{{")

    _set_key(monkeypatch, "k")
    _mock_youcom(monkeypatch, handler)
    monkeypatch.setattr(
        WebSearchTool, "_ddg_search",
        staticmethod(lambda q, n: [{"title": "DDG", "href": "https://d/1", "body": "b"}]),
    )

    res = await WebSearchTool().execute("q")
    assert res.raw["source"] == "duckduckgo"
    assert "DDG" in res.text


async def test_youcom_empty_returns_friendly(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": {}})

    _set_key(monkeypatch, "k")
    _mock_youcom(monkeypatch, handler)

    res = await WebSearchTool().execute("查询X")
    assert res.raw["source"] == "you.com"
    assert res.raw["count"] == 0
    assert "未找到" in res.text


async def test_youcom_hostile_shapes_no_crash(monkeypatch):
    body = {"results": {
        "web": ["str", 42, None, {"title": None, "url": None, "snippets": [None, "有效片段"]}],
        "news": "not-a-list",
    }}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    _set_key(monkeypatch, "k")
    _mock_youcom(monkeypatch, handler)

    res = await WebSearchTool().execute("q")
    assert res.raw["source"] == "you.com"
    assert res.raw["count"] == 1               # 非 dict / 非法项被跳过
    assert "有效片段" in res.text


async def test_ddg_failure_returns_error_result(monkeypatch):
    _set_key(monkeypatch, "")

    def _raise(q, n):
        raise RuntimeError("ddg down")

    monkeypatch.setattr(WebSearchTool, "_ddg_search", staticmethod(_raise))

    res = await WebSearchTool().execute("q")
    assert res.error and "web_search failed" in res.error
