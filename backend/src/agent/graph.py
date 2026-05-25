"""LangGraph graph construction."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, TYPE_CHECKING

from langgraph.graph import END, StateGraph

from src.agent.nodes import call_tools_node, plan_node, should_continue
from src.agent.prompts import (
    SYSTEM_PROMPT_GENERAL,
    SYSTEM_PROMPT_TRAVEL,
    build_kb_system_prompt,
)
from src.agent.state import AgentState
from src.infra.llm import CostTracker
from src.tools.base import ToolRegistry, build_default_registry

if TYPE_CHECKING:
    from src.settings_user import UserEmbeddingConfig, UserLLMConfig, UserRerankerConfig

Emitter = Callable[[dict[str, Any]], Awaitable[None]]


def build_graph(
    registry: ToolRegistry | None = None,
    emit: Emitter | None = None,
    *,
    kb=None,  # KB row from src.kb.models, or None for general chat mode
    llm_cfg: "UserLLMConfig | None" = None,
    embedding_cfg: "UserEmbeddingConfig | None" = None,
    reranker_cfg: "UserRerankerConfig | None" = None,
    kb_web_search_enabled: bool = False,
):
    """Wire up plan → call_tools loop, parameterized by KB context.

    Three modes (v2-M4):
      - kb=None: general chat — web_search-only toolset (v2-M5), neutral
        assistant prompt. No travel fallback (that was v1, fixed in v2-M4).
      - kb=<system travel demo KB>: travel agent (weather + restaurant_kb +
        amap + generate_travel_report skill, travel prompt). Reachable only
        by explicitly selecting "TravelGPT 演示库".
      - kb=<user KB>: KB-bound mode (search_kb + optional web_search per
        v2-M6, KB-specific prompt with optional score-tutorial section).

    v2-M1: `llm_cfg` and `embedding_cfg` are per-user overrides; None falls back
    to env-scoped defaults (so existing alice/bob keep working without
    visiting the settings page).

    v2-M6: `kb_web_search_enabled` is a per-user opt-in. When True AND a user
    KB is selected, also mount a tighter `WebSearchTool(default=3, cap=5)` and
    extend the KB prompt with score-interpretation + fallback guidance.

    v3-M4: `reranker_cfg` is a per-user opt-in cross-encoder reranker (default
    None = disabled). When set AND a user KB is selected, search_kb over-fetches
    candidates and reorders them via the configured /rerank endpoint. System
    KBs ignore reranker regardless. Hit `score` stays cosine so v2-M6 prompt
    threshold logic is preserved.
    """
    from src.kb.models import SYSTEM_TRAVEL_KB_ID

    if registry is None:
        registry = build_default_registry(
            kb=kb,
            embedding_cfg=embedding_cfg,
            reranker_cfg=reranker_cfg,
            user_kb_web_search_enabled=kb_web_search_enabled,
        )

    if kb is None:
        system_prompt = SYSTEM_PROMPT_GENERAL
        include_travel_skill = False
        include_kb_skill = False
    elif kb.id == SYSTEM_TRAVEL_KB_ID:
        system_prompt = SYSTEM_PROMPT_TRAVEL
        include_travel_skill = True
        include_kb_skill = False
    else:
        system_prompt = build_kb_system_prompt(
            kb.name,
            kb.description or "",
            with_web_search=kb_web_search_enabled,
        )
        include_travel_skill = False
        include_kb_skill = True

    cost = CostTracker()

    async def _noop_emit(_evt: dict[str, Any]) -> None:
        return None

    em = emit or _noop_emit

    # Use functools.partial instead of lambda to avoid coroutine issues
    from functools import partial

    g = StateGraph(AgentState)
    g.add_node(
        "plan",
        partial(
            plan_node,
            registry=registry,
            cost=cost,
            system_prompt=system_prompt,
            include_travel_skill=include_travel_skill,
            include_kb_skill=include_kb_skill,
            llm_cfg=llm_cfg,
        ),
    )
    g.add_node(
        "call_tools",
        partial(call_tools_node, registry=registry, emit=em, llm_cfg=llm_cfg),
    )

    g.set_entry_point("plan")
    g.add_conditional_edges("plan", should_continue, {"tools": "call_tools", "end": END})
    g.add_edge("call_tools", "plan")
    return g.compile(), cost
