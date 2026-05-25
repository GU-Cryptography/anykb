"""v3-M7: per-KB embedding + reranker config resolvers.

Each KB row may carry its own embedding / reranker credentials (set at KB
creation time via the upload dialog). If the KB row's columns are NULL we
fall back to the user-level cfg (mirrors v2-M1 / v3-M4 behavior).

Fall-back chain at ingest / search time:
    1) KB has full embedding cfg → use KB cfg
    2) Otherwise → user cfg (resolve_user_embedding)
    3) Both None → caller should 422 with "embedding not configured"

Reranker chain is similar but additionally:
    - System KB → always None (handled in KBSearchTool, not here)
    - Reranker is opt-in: KB cfg requires reranker_enabled=True
    - User-level fallback still applies when KB has no cfg
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from src.infra.crypto import decrypt
from src.settings_user.models import (
    UserEmbeddingConfig,
    UserRerankerConfig,
    resolve_user_embedding,
    resolve_user_reranker,
)

if TYPE_CHECKING:
    from src.auth.models import User
    from src.kb.models import KB


def _kb_embedding_is_configured(kb: "KB") -> bool:
    """KB-level embedding cfg considered present when provider/base_url/model
    are all set. api_key may be empty for self-hosted endpoints (Ollama)."""
    return bool(
        getattr(kb, "embedding_provider", None)
        and getattr(kb, "embedding_base_url", None)
        and getattr(kb, "embedding_model_override", None)
    )


def resolve_kb_embedding(
    kb: "KB", user: Optional["User"]
) -> Optional[UserEmbeddingConfig]:
    """Return the embedding cfg this KB should use for ingest / search.

    KB-level cfg wins over user-level cfg. Returns None when neither is
    configured — callers should 422 in that case.

    Note: `dim` is derived from `kb.vector_size` (already persisted on the KB
    at create time) when using KB-level cfg, since each KB pins its dim at
    creation and we never re-embed an existing KB.
    """
    if _kb_embedding_is_configured(kb):
        enc = getattr(kb, "embedding_api_key_enc", None)
        return UserEmbeddingConfig(
            provider=kb.embedding_provider or "",
            base_url=(kb.embedding_base_url or "").rstrip("/"),
            api_key=decrypt(enc) if enc else "",
            model=kb.embedding_model_override or "",
            dim=int(kb.vector_size or 0),
        )
    if user is None:
        return None
    return resolve_user_embedding(user)


def _kb_reranker_is_configured(kb: "KB") -> bool:
    if not bool(getattr(kb, "reranker_enabled", False)):
        return False
    return bool(
        getattr(kb, "reranker_provider", None)
        and getattr(kb, "reranker_base_url", None)
        and getattr(kb, "reranker_model", None)
    )


def resolve_kb_reranker(
    kb: "KB", user: Optional["User"]
) -> Optional[UserRerankerConfig]:
    """Return the reranker cfg this KB should use.

    System KB short-circuit lives in KBSearchTool (v3-M4 contract); here we
    only handle the cfg lookup. KB-level cfg wins; falls back to user cfg.
    """
    if _kb_reranker_is_configured(kb):
        enc = getattr(kb, "reranker_api_key_enc", None)
        return UserRerankerConfig(
            provider=kb.reranker_provider or "",
            base_url=(kb.reranker_base_url or "").rstrip("/"),
            api_key=decrypt(enc) if enc else "",
            model=kb.reranker_model or "",
        )
    if user is None:
        return None
    return resolve_user_reranker(user)
