"""Per-user LLM / embedding self-config (v2-M1).

Reads encrypted API keys from User row, exposes resolved configs to call sites.
v2-M2 adds BYOK enforcement helpers (gate.py).
"""
from __future__ import annotations

from .gate import require_user_embedding, require_user_llm
from .models import (
    UserEmbeddingConfig,
    UserLLMConfig,
    UserRerankerConfig,
    resolve_user_embedding,
    resolve_user_llm,
    resolve_user_reranker,
)

__all__ = [
    "UserEmbeddingConfig",
    "UserLLMConfig",
    "UserRerankerConfig",
    "resolve_user_embedding",
    "resolve_user_llm",
    "resolve_user_reranker",
    "require_user_embedding",
    "require_user_llm",
]
