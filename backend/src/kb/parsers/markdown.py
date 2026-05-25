"""Markdown / plain-text parser — minimal cleanup.

We don't strip markdown syntax (#, *, links). The embedding model handles it
fine and stripping makes downstream chunk previews less readable.
"""
from __future__ import annotations

import re


_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def parse_markdown(content: bytes) -> str:
    """Decode bytes → string, strip frontmatter & HTML comments, trim whitespace."""
    text = content.decode("utf-8", errors="replace")
    text = _FRONTMATTER_RE.sub("", text)
    text = _HTML_COMMENT_RE.sub("", text)
    # Collapse 3+ consecutive newlines to exactly two (preserve paragraph breaks).
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
