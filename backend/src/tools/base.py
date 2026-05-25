"""ToolRegistry — async tool abstraction with Anthropic-compatible schema."""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolResult:
    text: str
    latency_ms: int
    raw: Any = None
    error: str | None = None


class Tool(abc.ABC):
    name: str
    description: str
    input_schema: dict[str, Any]

    @abc.abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult: ...

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all_schemas(self) -> list[dict[str, Any]]:
        return [t.to_anthropic() for t in self._tools.values()]

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(text="", latency_ms=0, error=f"Unknown tool: {name}")
        start = time.perf_counter()
        try:
            result = await tool.execute(**args)
            if result.latency_ms == 0:
                result.latency_ms = int((time.perf_counter() - start) * 1000)
            return result
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                text="", latency_ms=int((time.perf_counter() - start) * 1000), error=str(exc)
            )


def build_default_registry(
    kb=None,
    embedding_cfg=None,
    *,
    reranker_cfg=None,
    user_kb_web_search_enabled: bool = False,
) -> ToolRegistry:
    """Build the agent's tool set based on which KB (if any) is active.

    Three cases (v2-M4):
      1. kb=None — general chat mode. Web search tool (v2-M5) so the LLM can
         pull real-time facts.
      2. kb=<system travel demo KB> — travel four-tool kit (weather + restaurant_kb
         + amap + the `generate_travel_report` skill that's wired in `graph.py`).
         Travel behavior is reachable only via this explicit selection.
      3. kb=<user KB> — KB-bound mode: `search_kb`, plus optionally a tighter
         `web_search` fallback (v2-M6, gated by `user_kb_web_search_enabled`).

    `embedding_cfg` (v2-M1): per-user embedding override, threaded through to
    `KBSearchTool` so query embedding uses the user's chosen provider. None =
    fall back to env config.

    `reranker_cfg` (v3-M4): per-user cross-encoder reranker override, threaded
    through to `KBSearchTool` for second-stage rerank of search hits. None =
    skip rerank (default). System KBs ignore this regardless.

    `user_kb_web_search_enabled` (v2-M6): per-user opt-in flag. When True and
    a user KB is selected, also mount `WebSearchTool(default=3, cap=5)` —
    tighter than the unbound-chat mount because KB chunks should remain the
    primary source.
    """
    from src.kb.models import SYSTEM_TRAVEL_KB_ID

    reg = ToolRegistry()

    # General chat mode — only web_search (v2-M5). LLM can answer from
    # pretraining knowledge, or call web_search for real-time facts. Keeps the
    # toolset minimal so the agent doesn't drift toward travel / KB tools when
    # no KB is selected.
    if kb is None:
        from src.tools.web_search import WebSearchTool

        reg.register(WebSearchTool())
        return reg

    # Built-in travel demo KB — keep v1 four-tool kit.
    if kb.id == SYSTEM_TRAVEL_KB_ID:
        from src.tools.amap_fallback import AmapFallbackTool
        from src.tools.restaurant_rag import RestaurantRagTool
        from src.tools.weather import WeatherTool

        reg.register(WeatherTool())
        reg.register(RestaurantRagTool())
        reg.register(AmapFallbackTool())
        return reg

    # User-created KB — search_kb plus optional tighter web_search fallback.
    from src.tools.kb_search import KBSearchTool

    reg.register(KBSearchTool(kb=kb, embedding_cfg=embedding_cfg, reranker_cfg=reranker_cfg))
    if user_kb_web_search_enabled:
        from src.tools.web_search import WebSearchTool

        # Tighter caps than general chat: KB is the primary source so web is
        # just a fallback for queries the KB doesn't cover.
        reg.register(WebSearchTool(max_results_default=3, max_results_cap=5))
    return reg
