"""Unit tests for v3-M1 context layering + token budget (memory-optimization).

Pure-unit: no DB, no network, no LLM. Covers estimate_tokens (English /
CJK / message blocks), build_context_sections (windowing, window_size=0
passthrough, L0 non-truncatable), build_layered_prompt (in-budget
passthrough, over-budget L5 truncation with L0 intact, budget<=0 skip),
and the tool_use/tool_result pairing invariant after truncation.
"""
from __future__ import annotations

import pytest

from src.agent.context_builder import (
    Section,
    build_layered_prompt,
    estimate_tokens,
    truncate_layer,
    window_messages,
)
from src.agent.prompts import (
    SYSTEM_PROMPT_GENERAL,
    SYSTEM_PROMPT_TRAVEL,
    build_context_sections,
    build_kb_system_prompt,
)


def _tool_use_msg(text: str, tid: str = "t1") -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "text", "text": text},
            {"type": "tool_use", "id": tid, "name": "search_kb", "input": {"query": "x"}},
        ],
    }


def _tool_result_msg(text: str, tid: str = "t1") -> dict:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}],
    }


def _orphans(msgs: list[dict]) -> list[dict]:
    """Return tool_result messages not preceded by a matching tool_use."""
    open_ids: set[str] = set()
    orphans: list[dict] = []
    for m in msgs:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                open_ids.add(b.get("id"))
            elif b.get("type") == "tool_result":
                if b.get("tool_use_id") not in open_ids:
                    orphans.append(m)
    return orphans


# --------------------------------------------------------------------------- #
# estimate_tokens
# --------------------------------------------------------------------------- #

def test_estimate_tokens_english():
    # 40 ASCII chars ≈ 10 tokens (chars/4).
    assert estimate_tokens("a" * 40) == 10


def test_estimate_tokens_cjk_higher_than_ascii():
    # Same char count, but CJK must estimate materially higher (~1.5 chars/token)
    # so the Chinese-first audience doesn't silently blow the budget.
    cjk = estimate_tokens("中" * 40)
    ascii_ = estimate_tokens("a" * 40)
    assert cjk > ascii_
    # 40 CJK chars → (40*2)//3 = 26 tokens.
    assert cjk == 26


def test_estimate_tokens_empty_string_floor():
    assert estimate_tokens("") == 1


def test_estimate_tokens_message_list_with_blocks():
    """content-as-list: text + tool_result payload + tool_use input all counted."""
    msgs = [
        {"role": "user", "content": "hello world"},
        _tool_use_msg("let me search"),
        _tool_result_msg("R" * 400),  # ~100 tokens of tool output
    ]
    tok = estimate_tokens(msgs)
    # Must count the large tool_result payload, not just the top-level text.
    assert tok >= 100


def test_estimate_tokens_ignores_non_dict_blocks():
    msgs = [{"role": "user", "content": ["not-a-dict", {"type": "text", "text": "ok"}]}]
    assert estimate_tokens(msgs) >= 1


# --------------------------------------------------------------------------- #
# build_context_sections
# --------------------------------------------------------------------------- #

def test_sections_l0_present_and_not_truncatable():
    sections = build_context_sections("SYSTEM", recent_messages=[], memory_window_size=10)
    l0 = next(s for s in sections if s.layer == 0)
    assert l0.role == "system"
    assert l0.content == "SYSTEM"
    assert l0.truncatable is False
    assert l0.section_key == "system_definition"


def test_sections_window_truncates_old_rounds():
    # 12 rounds = 24 messages; window=3 rounds keeps ~6 newest.
    msgs = []
    for i in range(12):
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    sections = build_context_sections("SYS", recent_messages=msgs, memory_window_size=3)
    l5 = next(s for s in sections if s.layer == 5)
    assert isinstance(l5.content, list)
    assert len(l5.content) <= 6
    # Newest turn survives, oldest dropped.
    assert l5.content[-1]["content"] == "a11"
    assert all(m.get("content") != "q0" for m in l5.content)


def test_sections_window_zero_keeps_everything():
    """PRD §11 backward-compat: memory_window_size=0 keeps full history verbatim."""
    msgs = []
    for i in range(30):
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    sections = build_context_sections("SYS", recent_messages=msgs, memory_window_size=0)
    l5 = next(s for s in sections if s.layer == 5)
    assert len(l5.content) == 60  # nothing dropped


def test_sections_l5_marked_truncatable():
    sections = build_context_sections("SYS", recent_messages=[{"role": "user", "content": "hi"}])
    l5 = next(s for s in sections if s.layer == 5)
    assert l5.truncatable is True
    assert l5.section_key == "recent_messages"


def test_sections_none_messages_safe():
    sections = build_context_sections("SYS", recent_messages=None)
    l5 = next(s for s in sections if s.layer == 5)
    assert l5.content == []


# --------------------------------------------------------------------------- #
# window_messages — tool pairing invariant
# --------------------------------------------------------------------------- #

def test_window_never_starts_on_orphan_tool_result():
    # Turn 1 is a tool round; a naive tail slice could start on the tool_result.
    msgs = [
        {"role": "user", "content": "q1"},
        _tool_use_msg("searching", "t1"),
        _tool_result_msg("chunk data", "t1"),
        {"role": "assistant", "content": "answer1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "answer2"},
    ]
    # Force the naive cut to land right on the tool_result (index 2).
    windowed = window_messages(msgs, 4)
    assert _orphans(windowed) == []
    # First surviving message must be a real user turn, not a tool_result.
    first = windowed[0]
    assert first["role"] == "user"
    assert not (
        isinstance(first["content"], list)
        and any(b.get("type") == "tool_result" for b in first["content"])
    )


def test_window_keeps_inflight_turn_whole_when_longer_than_window():
    # A single unfinished turn longer than the window must survive intact
    # rather than be split into an invalid orphan.
    msgs = [
        {"role": "user", "content": "big task"},
        _tool_use_msg("step1", "t1"),
        _tool_result_msg("r1", "t1"),
        _tool_use_msg("step2", "t2"),
        _tool_result_msg("r2", "t2"),
    ]
    windowed = window_messages(msgs, 2)
    assert _orphans(windowed) == []
    assert windowed[0]["content"] == "big task"


def test_window_noop_when_within_size():
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert window_messages(msgs, 10) == msgs


# --------------------------------------------------------------------------- #
# build_layered_prompt
# --------------------------------------------------------------------------- #

def test_layered_passthrough_within_budget():
    msgs = [{"role": "user", "content": "hello"}]
    sections = build_context_sections("SYSTEM PROMPT", recent_messages=msgs)
    layered = build_layered_prompt(sections, total_budget=8000)
    assert layered.over_budget is False
    assert layered.system_text == "SYSTEM PROMPT"
    assert layered.system_blocks == [{"type": "text", "text": "SYSTEM PROMPT"}]
    assert layered.messages == msgs


def test_layered_truncates_l5_keeps_l0_intact_when_over_budget():
    # Huge history forces L5 truncation; L0 must remain untouched.
    big_system = "系统定义 " * 20
    msgs = []
    for i in range(50):
        msgs.append({"role": "user", "content": "问题内容 " * 30})
        msgs.append({"role": "assistant", "content": "回答内容 " * 30})
    sections = build_context_sections(big_system, recent_messages=msgs, memory_window_size=0)
    before = len(next(s for s in sections if s.layer == 5).content)
    layered = build_layered_prompt(sections, total_budget=500)
    # L0 fully preserved in output.
    assert big_system.strip() in layered.system_text
    # L5 got shorter.
    assert len(layered.messages) < before
    # No orphaned tool_result introduced (there were none, must stay none).
    assert _orphans(layered.messages) == []


def test_layered_truncation_preserves_tool_pairs():
    big_system = "SYS"
    msgs = []
    # 20 tool rounds, each: user q, assistant tool_use, user tool_result, assistant answer.
    for i in range(20):
        tid = f"t{i}"
        msgs.append({"role": "user", "content": f"q{i} " + "填充" * 40})
        msgs.append(_tool_use_msg("searching " + "填充" * 40, tid))
        msgs.append(_tool_result_msg("result " + "填充" * 40, tid))
        msgs.append({"role": "assistant", "content": f"answer{i} " + "填充" * 40})
    sections = build_context_sections(big_system, recent_messages=msgs, memory_window_size=0)
    layered = build_layered_prompt(sections, total_budget=800)
    assert layered.messages  # not emptied
    assert _orphans(layered.messages) == []
    # First surviving message is a clean user turn (not a tool_result).
    first = layered.messages[0]
    assert not (
        isinstance(first["content"], list)
        and any(b.get("type") == "tool_result" for b in first["content"])
    )


def test_layered_budget_zero_skips_enforcement():
    """total_budget<=0 → never flagged over_budget, no truncation."""
    msgs = [{"role": "user", "content": "问题" * 5000}]
    sections = build_context_sections("SYS", recent_messages=msgs, memory_window_size=0)
    layered = build_layered_prompt(sections, total_budget=0)
    assert layered.over_budget is False
    assert len(layered.messages) == 1
    assert layered.messages[0]["content"] == "问题" * 5000


def test_layered_system_and_messages_split_by_role():
    sections = [
        Section(layer=0, role="system", content="S0", section_key="system_definition"),
        Section(
            layer=5,
            role="user",
            content=[{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}],
            truncatable=True,
            budget=3000,
            section_key="recent_messages",
        ),
    ]
    layered = build_layered_prompt(sections, total_budget=8000)
    assert layered.system_text == "S0"
    assert layered.messages == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]


def test_layered_over_budget_flag_when_untruncatable_exceeds():
    # A single unfinished turn that alone blows the budget: cannot truncate
    # further (would orphan), so over_budget stays True but request is valid.
    msgs = [
        {"role": "user", "content": "任务" * 2000},
        _tool_use_msg("x" * 4000, "t1"),
        _tool_result_msg("y" * 4000, "t1"),
    ]
    sections = build_context_sections("SYS", recent_messages=msgs, memory_window_size=0)
    layered = build_layered_prompt(sections, total_budget=300)
    assert layered.over_budget is True
    assert _orphans(layered.messages) == []


# --------------------------------------------------------------------------- #
# Regression: three modes' prompt semantics unchanged by layering (v3-M1)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "system_prompt",
    [
        SYSTEM_PROMPT_GENERAL,
        SYSTEM_PROMPT_TRAVEL,
        build_kb_system_prompt("测试库", "库描述", with_web_search=True),
    ],
    ids=["general", "travel", "kb"],
)
def test_mode_prompt_equivalence_within_window(system_prompt):
    """M1 only re-plumbs assembly: within the window, the LLM must receive
    exactly the same system text (both API shapes) and the same messages
    as before the refactor."""
    msgs = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，有什么可以帮你？"},
        {"role": "user", "content": "帮我查一下 Docker 的文档"},
    ]
    sections = build_context_sections(
        system_prompt, recent_messages=msgs, memory_window_size=10
    )
    layered = build_layered_prompt(sections, total_budget=8000)
    # OpenAI-compat path: single system string, verbatim.
    assert layered.system_text == system_prompt
    # Anthropic path: single text block, verbatim (with_cache_control is
    # applied downstream in plan_node, same as before).
    assert layered.system_blocks == [{"type": "text", "text": system_prompt}]
    # Conversation untouched within window/budget.
    assert layered.messages == msgs
    assert layered.over_budget is False


# --------------------------------------------------------------------------- #
# truncate_layer direct
# --------------------------------------------------------------------------- #

def test_truncate_layer_returns_new_list_without_mutating():
    original = [
        Section(
            layer=5,
            role="user",
            content=[{"role": "user", "content": "老" * 500} for _ in range(4)],
            truncatable=True,
            budget=50,
            section_key="recent_messages",
        )
    ]
    before_len = len(original[0].content)
    result = truncate_layer(original, 5, target_tokens=50)
    # Original section object content not mutated in place.
    assert len(original[0].content) == before_len
    assert len(result[0].content) <= before_len


def test_truncate_layer_ignores_non_truncatable():
    sections = [
        Section(layer=0, role="system", content="S", truncatable=False, budget=400),
    ]
    result = truncate_layer(sections, 0, target_tokens=1)
    assert result[0].content == "S"
