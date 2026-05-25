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
