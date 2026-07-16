"""Context layering and token budget management (v3-M1 memory-optimization).

Provides Section dataclass and build_layered_prompt() for constructing
layered system + messages prompts with token budget enforcement.

Usage::

    sections = build_context_sections(   # lives in src.agent.prompts
        system_prompt_text=system_prompt,
        recent_messages=state["messages"],
        memory_window_size=settings.memory_window_size,
    )
    layered = build_layered_prompt(sections, total_budget=settings.context_total_budget)
    # layered.system_text   → OpenAI-compatible system string
    # layered.system_blocks → Anthropic system blocks
    # layered.messages      → conversation messages
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

# Token budget per layer (heuristic estimate, see estimate_tokens).
# Used as soft caps for truncatable layers during over-budget recovery.
# M1: only system_definition and recent_messages are active.
# L1-L4 (user_profile, long_term_memory, task_context, early_summary)
# are reserved for M2/M3.
CONTEXT_BUDGET: dict[str, int] = {
    "system_definition": 400,
    "user_profile": 200,
    "long_term_memory": 300,
    "task_context": 200,
    "early_summary": 300,
    "recent_messages": 3000,
    "tool_results": 1500,
}
TOTAL_BUDGET = 8000

# CJK codepoint ranges for token estimation. Chinese packs ~1.5 chars per
# token vs ~4 for ASCII; this product's primary audience writes Chinese,
# so a flat len//4 would under-count real usage by ~2.6x.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x9FFF),    # CJK punctuation, kana, CJK Unified Ideographs (incl. ext A)
    (0xAC00, 0xD7AF),    # Hangul syllables
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
    (0xFF00, 0xFFEF),    # Fullwidth / halfwidth forms
    (0x20000, 0x2FA1F),  # CJK Unified Ideographs extensions B-F
)


@dataclass
class Section:
    """A single context layer.

    Attributes:
        layer: Ordinal (0-6) determining position in the final prompt.
        role:  "system" | "user" | "assistant" | "tool".
        content: Text string (for system) or list of message dicts (for conversation).
        truncatable: Whether this layer can be shortened when over budget.
        budget: Token budget for this layer.
        section_key: Human-readable name matching CONTEXT_BUDGET keys.
    """

    layer: int
    role: str
    content: str | list[dict[str, Any]]
    truncatable: bool = False
    budget: int = 0
    section_key: str = ""


@dataclass
class LayeredPrompt:
    """Result of assembling sections into a prompt ready for the LLM call.

    Attributes:
        system_text: Combined system sections as a single string (OpenAI-compatible).
        system_blocks: List of ``{"type": "text", "text": "..."}`` blocks (Anthropic).
        messages: Conversation messages (L5-L6) for the LLM call.
        total_tokens: Estimated token count.
        over_budget: True if total_tokens exceeds *total_budget* after truncation.
    """

    system_text: str = ""
    system_blocks: list[dict[str, str]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    total_tokens: int = 0
    over_budget: bool = False


def _text_tokens(text: str) -> int:
    """CJK-aware chars→tokens heuristic (no tiktoken dependency).

    ASCII/Latin ≈ 4 chars/token; CJK ≈ 1.5 chars/token (rounded via *2//3).
    A proper tokenizer can be swapped in at M2/M3 without touching callers.
    """
    cjk = 0
    for ch in text:
        cp = ord(ch)
        for lo, hi in _CJK_RANGES:
            if lo <= cp <= hi:
                cjk += 1
                break
    return (cjk * 2) // 3 + (len(text) - cjk) // 4


def _block_tokens(block: dict[str, Any]) -> int:
    """Estimate tokens for one content block (text / tool_use / tool_result)."""
    total = 0
    text = block.get("text")
    if isinstance(text, str):
        total += _text_tokens(text)
    # tool_result payload lives under "content" (str, or nested block list) —
    # often the largest part of a tool-heavy conversation, must be counted.
    inner = block.get("content")
    if isinstance(inner, str):
        total += _text_tokens(inner)
    elif isinstance(inner, list):
        total += sum(_block_tokens(b) for b in inner if isinstance(b, dict))
    # tool_use arguments.
    tool_input = block.get("input")
    if isinstance(tool_input, dict) and tool_input:
        total += _text_tokens(json.dumps(tool_input, ensure_ascii=False))
    return total


def estimate_tokens(text_or_messages: str | list[dict[str, Any]]) -> int:
    """Rough token estimate without tiktoken.

    CJK-aware (~1.5 chars/token for CJK, ~4 for the rest).  For message
    lists, sums across text blocks, tool_result payloads and tool_use
    inputs so tool-heavy turns are not under-counted.
    """
    if isinstance(text_or_messages, str):
        return max(1, _text_tokens(text_or_messages))

    total = 0
    for msg in text_or_messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += _text_tokens(content)
        elif isinstance(content, list):
            total += sum(_block_tokens(b) for b in content if isinstance(b, dict))
    return max(1, total)


def _estimate_tokens_for_sections(sections: list[Section]) -> int:
    """Estimate total tokens across all *sections*."""
    return sum(estimate_tokens(sec.content) for sec in sections)


def _is_turn_start(msg: dict[str, Any]) -> bool:
    """True if *msg* is a safe truncation boundary (a plain user turn).

    A user message whose content contains tool_result blocks is NOT a
    boundary: it must stay glued to the preceding assistant tool_use
    message, otherwise both Anthropic and OpenAI-compat APIs reject the
    request (HTTP 400, orphan tool_result / tool message).
    """
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        return False
    return True


def _next_turn_start(msgs: list[dict[str, Any]], start: int) -> int | None:
    """Index of the first turn boundary at or after *start*, or None."""
    for i in range(start, len(msgs)):
        if _is_turn_start(msgs[i]):
            return i
    return None


def window_messages(msgs: list[dict[str, Any]], max_msgs: int) -> list[dict[str, Any]]:
    """Sliding window over *msgs* keeping ~*max_msgs* newest, turn-aligned.

    A naive ``msgs[-max_msgs:]`` can cut between an assistant tool_use and
    its user tool_result, or start the history on an assistant message —
    both rejected by the LLM APIs. The window instead starts at the first
    turn boundary at/after the naive cut. If the tail has no boundary at
    all (one in-flight turn longer than the window), fall back to the
    latest boundary before the cut so the whole turn survives intact —
    slightly over the window is safer than an HTTP 400.
    """
    if max_msgs <= 0 or len(msgs) <= max_msgs:
        return list(msgs)
    cut = len(msgs) - max_msgs
    nxt = _next_turn_start(msgs, cut)
    if nxt is not None:
        return msgs[nxt:]
    for i in range(cut - 1, -1, -1):
        if _is_turn_start(msgs[i]):
            return msgs[i:]
    return list(msgs)


def truncate_layer(
    sections: list[Section],
    layer_idx: int,
    target_tokens: int,
) -> list[Section]:
    """Truncate a specific layer to fit within *target_tokens*.

    Only acts on truncatable layers whose content is a list of message
    dicts (e.g. L5).  Drops whole conversation turns from the front
    (oldest first) — never splits a tool_use/tool_result pair and never
    drops the final in-flight turn, even if that leaves the layer over
    budget (an over-budget request beats an invalid one).

    Returns a new list with the truncated section replaced.
    """
    result = list(sections)
    for i, sec in enumerate(result):
        if sec.layer != layer_idx or not sec.truncatable:
            continue
        if not isinstance(sec.content, list):
            continue

        content: list[dict[str, Any]] = list(sec.content)
        while estimate_tokens(content) > target_tokens:
            nxt = _next_turn_start(content, 1)
            if nxt is None:
                break  # only the in-flight turn left — keep it whole
            content = content[nxt:]

        result[i] = replace(sec, content=content)
    return result


def build_layered_prompt(
    sections: list[Section],
    total_budget: int = TOTAL_BUDGET,
) -> LayeredPrompt:
    """Combine *sections* into a unified prompt, enforcing token budget.

    Truncation order when over budget:
        1. L6 (tool_results) — drop oldest.
        2. L5 (recent_messages) — drop oldest turns.
    L0-L4 are never truncated.

    When *total_budget* is <= 0 budget enforcement is skipped entirely
    (config escape hatch, independent from ``memory_window_size=0`` which
    disables windowing/truncation at the section level).
    """
    enforce_budget = total_budget > 0
    total_tokens = _estimate_tokens_for_sections(sections)
    over_budget = enforce_budget and total_tokens > total_budget

    if over_budget:
        # Truncate in priority order: L6 first, then L5.
        for layer_idx in (6, 5):
            layer_budget = sum(
                sec.budget for sec in sections if sec.layer == layer_idx
            )
            if layer_budget <= 0:
                continue
            sections = truncate_layer(sections, layer_idx, layer_budget)
            total_tokens = _estimate_tokens_for_sections(sections)
            if total_tokens <= total_budget:
                over_budget = False
                break

    # Build system blocks + messages.
    system_text_parts: list[str] = []
    system_blocks: list[dict[str, str]] = []
    messages: list[dict[str, Any]] = []

    for sec in sections:
        if sec.role == "system":
            if isinstance(sec.content, str):
                system_text_parts.append(sec.content)
                system_blocks.append({"type": "text", "text": sec.content})
            elif isinstance(sec.content, list):
                # M1 never produces list-form system sections (L0 is always a
                # plain string). Accepted for forward-compat with pre-built
                # {"type": "text", "text": ...} blocks only; other shapes are
                # an upstream contract violation, not silently coerced here.
                for item in sec.content:
                    text = item.get("text") if isinstance(item, dict) else None
                    if isinstance(text, str) and text:
                        system_text_parts.append(text)
                        system_blocks.append({"type": "text", "text": text})
        elif isinstance(sec.content, list):
            messages.extend(sec.content)
        elif isinstance(sec.content, str):
            messages.append({"role": sec.role, "content": sec.content})

    return LayeredPrompt(
        system_text="\n\n".join(system_text_parts),
        system_blocks=system_blocks,
        messages=messages,
        total_tokens=total_tokens,
        over_budget=over_budget,
    )
