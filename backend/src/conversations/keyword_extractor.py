"""Real-time keyword triggers for long-term memory extraction (v3-M3, PRD §5.4).

Pure, zero-LLM rule layer: a small set of Chinese regexes that decide *whether*
a user message is worth extracting a long-term memory from. Matching is fast
(<1ms) and side-effect free; the actual extraction (turning the raw message into
a ``{type, content, importance}`` record) is done afterwards by the session LLM
in ``long_term_memory.schedule_keyword_extraction`` — this module never calls an
LLM and never writes anything.

Two rule outcomes:
  * a *storable* category (profile / preference / fact / explicit) → the caller
    should ask the LLM to format+store a memory, biased by the hit categories.
  * ``task_boundary`` → a task-switch signal only (PRD §5.4 "不存记忆"); M3 does
    NOT persist task_state (that's L3, out of scope), so a message that ONLY hits
    task_boundary yields no extraction.

Regexes intentionally start from the PRD's list and stay conservative (high
precision over recall): a false positive costs one LLM formatting call that the
LLM can still veto by returning an empty object; a false negative just misses
one memory that the session-end extraction (PRD §5.5) can still catch later.
"""
from __future__ import annotations

import re

# (pattern, category). Order matters only for readability — detect() returns all
# matches. ``task_boundary`` is a non-storable signal (see module docstring).
PATTERNS: list[tuple[str, str]] = [
    (r"(我是|我叫|我做|我搞|我从事|我的职业|我是一名|我是个)", "profile"),
    (r"(我喜欢|我习惯|我偏好|我讨厌|我不用|从来不用|我倾向|我更喜欢|我一般用|我平时用)", "preference"),
    (r"(我们|公司|团队|项目|我司).{0,12}(用|使用|部署|采用|基于|跑在)", "fact"),
    (r"(搞定了|搞定啦|完成了|做完了|好了[，,]?\s*下一步|这个先这样|换个话题|接下来我们)", "task_boundary"),
    (r"(帮我记住|记一下|记住这个|记住我|以后记得|请记住|帮我记一下)", "explicit"),
]

# Categories that should drive an extraction. task_boundary is excluded.
STORABLE_CATEGORIES: frozenset[str] = frozenset(
    {"profile", "preference", "fact", "explicit"}
)

_COMPILED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p), cat) for p, cat in PATTERNS
]


def detect(text: str) -> list[str]:
    """Return the distinct categories whose pattern matches *text*.

    Includes ``task_boundary`` when hit (callers decide how to treat it). Order
    follows PATTERNS declaration order; each category appears at most once.
    Empty / whitespace input → ``[]``.
    """
    if not text or not text.strip():
        return []
    hits: list[str] = []
    for rx, cat in _COMPILED:
        if cat not in hits and rx.search(text):
            hits.append(cat)
    return hits


def storable_categories(text: str) -> list[str]:
    """Categories from :func:`detect` that warrant persisting a memory.

    Drops ``task_boundary`` (M3 doesn't store task_state). Empty result means
    "no keyword hit worth an extraction call" — the caller should not invoke the
    LLM at all.
    """
    return [c for c in detect(text) if c in STORABLE_CATEGORIES]
