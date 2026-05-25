"""L1 — input sanitization. Block obvious dangerous prompts."""
from __future__ import annotations

import re

DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b",
    r"<script[^>]*>",
    r"javascript:",
    r"\bexec\s*\(",
    r"\beval\s*\(",
    r"DROP\s+TABLE",
    r"DELETE\s+FROM",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in DANGEROUS_PATTERNS]


def sanitize_user_input(text: str) -> tuple[str, str | None]:
    """Return (cleaned_text, blocked_reason). If blocked, cleaned_text is empty."""
    if not text:
        return "", None
    if len(text) > 2000:
        return "", "输入过长 (>2000 字符)"
    for pat in _COMPILED:
        if pat.search(text):
            return "", f"输入包含危险模式: {pat.pattern}"
    return text.strip(), None
