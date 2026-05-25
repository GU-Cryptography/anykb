"""web_search — DuckDuckGo (no API key required) general-purpose web search.

Mounted in two places (v2-M6):
- Unbound chat mode (v2-M5): WebSearchTool() with default=5, cap=10 — agent is
  the primary information source so web is liberal.
- KB+web mode (v2-M6, opt-in per user): WebSearchTool(default=3, cap=5) — KB
  chunks are the primary source so web is just a tighter fallback.

Implementation notes:
- `ddgs` (formerly `duckduckgo-search`) is a sync iterator-based client. We
  wrap each call in `asyncio.to_thread` so we don't block the event loop.
- No retry / cache layers — `ddgs` retries internally, and individual dev
  workloads don't approach the limit. Add a layer if production usage hits
  rate limits.
"""
from __future__ import annotations

import asyncio
from typing import Any

from src.tools.base import Tool, ToolResult


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

        def _run() -> list[dict]:
            from ddgs import DDGS

            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=n))

        try:
            results = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(text="", latency_ms=0, error=f"web_search failed: {exc}")

        if not results:
            return ToolResult(
                text=f"未找到关于 '{query}' 的网络结果。",
                latency_ms=0,
                raw={"count": 0, "query": query},
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
            raw={"count": len(results), "query": query},
        )
