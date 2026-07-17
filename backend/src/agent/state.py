"""Agent state types."""
from __future__ import annotations

from typing import Any, TypedDict


class ToolCallRecord(TypedDict):
    id: str
    name: str
    input: dict[str, Any]
    result: str | None
    latency_ms: int | None
    error: str | None


class AgentState(TypedDict, total=False):
    messages: list[dict[str, Any]]           # Anthropic messages history
    pending_tool_calls: list[dict[str, Any]] # tool_use blocks awaiting execution
    tool_call_log: list[ToolCallRecord]      # observable timeline for ThinkingChain UI
    final_report: str | None
    iterations: int                          # plan loop guard
    cost_usd: float
    # v3-M2 memory-optimization: identify the session so plan_node can read the
    # early-summary (L4) for this conversation. Both optional — when absent
    # (e.g. old frontend not passing conversation_id) plan_node simply omits L4,
    # falling back to exact M1 behavior.
    conversation_id: str                     # source conversation (for L4 summary)
    user_id: str                             # owner (for Redis/PG summary lookup)
    # Per-request cache of the L4 early summary: plan_node fetches it once on
    # the first iteration and stores it here (possibly "") so tool-loop
    # iterations don't repeat the Redis/PG read. Absent = not fetched yet.
    early_summary: str
    # v3-M3 memory-optimization: per-request cache of the L1 user profile and
    # L2 long-term memories. plan_node fetches BOTH once on the first iteration
    # (keyed off "long_term_memory" not being in state yet) and stores them so
    # later tool-loop iterations skip the Redis/Milvus/PG reads — same
    # once-per-request pattern as early_summary. user_profile may be an
    # all-empty dict (→ L1 omitted); long_term_memory may be [] (→ L2 omitted).
    user_profile: dict[str, Any]
    long_term_memory: list[dict[str, Any]]
