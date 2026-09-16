"""web_search — 通用互联网搜索。首选 You.com Search API，未配置时回退 DuckDuckGo。

来源优先级（v3-M7）：
- 配置了 `YDC_API_KEY` → 走 You.com Search API（GET https://ydc-index.io/v1/search，
  X-API-Key 鉴权），返回带来源链接的网页 + 新闻结果；
- 未配置 Key，或 You.com 调用失败 → 回退 DuckDuckGo（`ddgs`，无需 Key）。
  这样即便没有 Key，web_search 行为与此前完全一致，不产生任何回归。

Mounted in two places (v2-M6):
- Unbound chat mode (v2-M5): WebSearchTool() with default=5, cap=10 — agent is
  the primary information source so web is liberal.
- KB+web mode (v2-M6, opt-in per user): WebSearchTool(default=3, cap=5) — KB
  chunks are the primary source so web is just a tighter fallback.

Implementation notes:
- You.com 调用用 `httpx.AsyncClient`（与 amap_fallback / 其它工具一致），原生异步。
- `ddgs` (formerly `duckduckgo-search`) is a sync iterator-based client. We
  wrap each call in `asyncio.to_thread` so we don't block the event loop.
- 任何失败都不会抛出：You.com 出错回退 DuckDuckGo，DuckDuckGo 出错返回错误态
  ToolResult，绝不让 web_search 故障影响上层 Agent。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from src.settings import get_settings
from src.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

# You.com Search API 地址（团队约定端点）
YOUCOM_ENDPOINT = "https://ydc-index.io/v1/search"
# 团队约定：对 You.com 主机的请求需带此 User-Agent（lowercased owner-repo slug）
YOUCOM_USER_AGENT = "youdotcom-integration/gu-cryptography-anykb"


class WebSearchTool(Tool):
    name = "web_search"

    def __init__(
        self,
        *,
        max_results_default: int = 5,
        max_results_cap: int = 10,
    ) -> None:
        """Per-mount config.

        - max_results_default: value used when LLM omits max_results (also what
          gets advertised in the schema's `default`).
        - max_results_cap: hard upper bound; LLM can't ask for more (clamp +
          schema `maximum`).
        """
        self._default = max(1, int(max_results_default))
        self._cap = max(self._default, int(max_results_cap))
        # Recompute per-instance description + input_schema so the LLM sees the
        # tighter limits in the KB mode mount.
        self.description = (
            "搜索互联网获取实时信息或模型预训练之外的事实。"
            "适合查询：最新新闻、近期数据、长尾事实、模型不掌握的内容。"
            f"返回最多 {self._cap} 条结果（默认 {self._default}），每条含标题、URL、摘要。"
            "回答用户时必须在内容中标注引用的 URL 来源。"
        )
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词。越具体越好；中英文都行。",
                },
                "max_results": {
                    "type": "integer",
                    "description": f"返回结果数 (1-{self._cap})，默认 {self._default}",
                    "default": self._default,
                    "minimum": 1,
                    "maximum": self._cap,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, max_results: int | None = None) -> ToolResult:
        # Clamp max_results defensively; LLMs sometimes pass strings or out-of-range ints.
        if max_results is None:
            n = self._default
        else:
            try:
                n = max(1, min(int(max_results), self._cap))
            except (TypeError, ValueError):
                n = self._default

        api_key = get_settings().ydc_api_key
        source = "duckduckgo"
        results: list[dict] = []

        # 首选 You.com；失败则回退 DuckDuckGo，保证可用性不回退。
        if api_key:
            try:
                results = await self._youcom_search(query, n, api_key)
                source = "you.com"
            except Exception as exc:  # noqa: BLE001 — 出错回退，不向上抛
                logger.warning("web_search: You.com 调用失败，回退 DuckDuckGo: %s", exc)

        if source == "duckduckgo":
            try:
                results = await asyncio.to_thread(self._ddg_search, query, n)
            except Exception as exc:  # noqa: BLE001
                return ToolResult(text="", latency_ms=0, error=f"web_search failed: {exc}")

        if not results:
            return ToolResult(
                text=f"未找到关于 '{query}' 的网络结果。",
                latency_ms=0,
                raw={"count": 0, "query": query, "source": source},
            )

        lines: list[str] = []
        for i, r in enumerate(results, 1):
            title = (r.get("title") or "").strip()[:120]
            url = (r.get("href") or r.get("url") or "").strip()
            body = (r.get("body") or "").strip()[:240]
            lines.append(f"[{i}] {title}\n    URL: {url}\n    摘要: {body}")

        return ToolResult(
            text="\n\n".join(lines),
            latency_ms=0,
            raw={"count": len(results), "query": query, "source": source},
        )

    async def _youcom_search(self, query: str, n: int, api_key: str) -> list[dict]:
        """调用 You.com Search API，归一化为 {title, href, body} 列表。

        响应结构 `{"results": {"web": [...], "news": [...]}}`；news 可能缺失，
        每条结果除 url/title/description/snippets 外的字段均视为可选，防御式读取。
        非 2xx / 网络异常抛出，由 execute 捕获后回退 DuckDuckGo（不回显 Key）。
        """
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                YOUCOM_ENDPOINT,
                params={"query": query, "count": n},
                headers={"X-API-Key": api_key, "User-Agent": YOUCOM_USER_AGENT},
            )
        if resp.status_code != 200:
            # 不回显响应体，避免泄露账号信息；401 鉴权 / 429 限流 / 5xx 服务端
            raise RuntimeError(f"You.com API 返回状态码 {resp.status_code}")

        payload = resp.json()
        raw_results = payload.get("results") or {}
        items: list[dict] = []
        for bucket in ("web", "news"):
            entries = raw_results.get(bucket)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                snippets = entry.get("snippets")
                snippet_text = ""
                if isinstance(snippets, list):
                    snippet_text = " ".join(s for s in snippets if isinstance(s, str))
                body = entry.get("description") or snippet_text
                items.append(
                    {
                        "title": entry.get("title") or "",
                        "href": entry.get("url") or "",
                        "body": body or "",
                    }
                )
        # web 与 news 各返回最多 n 条，按总数截断，与 max_results 语义对齐
        return items[:n]

    @staticmethod
    def _ddg_search(query: str, n: int) -> list[dict]:
        from ddgs import DDGS

        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=n))
