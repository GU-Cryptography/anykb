"""Recursive text chunker — paragraph → sentence → char fallback.

Sizes are in characters, not tokens. Rationale:
  - BGE-M3 tokenizer-aware sizing requires loading the tokenizer in-process
    (~heavy). Chars are a decent proxy: Chinese ~1.5 char/token, English
    ~4 char/token, mixed ~2-3 char/token. With target=1500 chars we land in
    the 500-1000 token band most of the time — well under the 8K context of
    BGE-M3 and most modern embedding models.
  - Adjust via env if you need to be precise; current defaults are conservative.

Public API:
    chunk_text(text, target=1500, max_size=1800, overlap=150) -> list[str]
"""
from __future__ import annotations

import re


# Sentence boundary: . ! ? 。 ！ ？ followed by whitespace OR end.
# (?<=...) keeps the punctuation attached to the preceding sentence.
_SENT_RE = re.compile(r"(?<=[.!?。！？])(?=\s|$)")


def chunk_text(
    text: str,
    *,
    target: int = 1500,
    max_size: int = 1800,
    overlap: int = 150,
) -> list[str]:
    """Split text into overlapping chunks at natural boundaries.

    Algorithm:
      1. Split on blank lines (paragraphs).
      2. Any paragraph > max_size: re-split by sentence punctuation.
      3. Any sentence > max_size: hard-split by character.
      4. Greedily pack atoms into chunks ≤ target. When closing a chunk,
         carry the last ~overlap chars of atoms into the next chunk for
         continuity at boundaries.
    """
    if not text or not text.strip():
        return []

    atoms = _atomize(text, max_size=max_size)
    if not atoms:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    sep = "\n\n"
    sep_len = len(sep)

    for atom in atoms:
        atom_len = len(atom)
        join_cost = sep_len if current else 0

        if current and current_len + join_cost + atom_len > target:
            chunks.append(sep.join(current))
            # Build overlap by taking trailing atoms whose total len ≤ overlap.
            tail: list[str] = []
            tail_len = 0
            for a in reversed(current):
                if tail and tail_len + sep_len + len(a) > overlap:
                    break
                tail.insert(0, a)
                tail_len += len(a) + (sep_len if len(tail) > 1 else 0)
            current = tail + [atom]
            current_len = sum(len(a) for a in current) + sep_len * (len(current) - 1)
        else:
            current.append(atom)
            current_len += join_cost + atom_len

    if current:
        chunks.append(sep.join(current))

    return chunks


def _atomize(text: str, *, max_size: int) -> list[str]:
    """Break text into ≤max_size atoms: paragraphs, sentences, or char slices."""
    atoms: list[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_size:
            atoms.append(para)
            continue
        # paragraph too big — split into sentences
        for sent in _SENT_RE.split(para):
            sent = sent.strip()
            if not sent:
                continue
            if len(sent) <= max_size:
                atoms.append(sent)
                continue
            # sentence still too big — hard slice
            for i in range(0, len(sent), max_size):
                atoms.append(sent[i : i + max_size])
    return atoms
