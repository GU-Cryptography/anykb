"""Reranker provider abstraction (v3-M4).

Second-stage retrieval booster. Takes a query + a list of candidate documents
and returns reordered (idx, relevance_score) pairs via a cross-encoder model.

Why a separate module from embedding.py?
  - Different protocol (Cohere /rerank shape, not OpenAI /embeddings).
  - Different cost profile (one call per query, not per doc).
  - Opt-in: when no `cfg` is provided, callers get a no-op passthrough that
    returns the original order — there is no `.env` fallback this milestone.

The Cohere /rerank request shape is the de-facto standard — SiliconFlow,
Cohere, Jina, TEI, vLLM with reranker plugins all accept the same JSON.
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from src.settings_user.models import UserRerankerConfig


# ---------------------------------------------------------------------------
# Provider presets
# ---------------------------------------------------------------------------
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "BAAI/bge-reranker-v2-m3",
        "protocol": "cohere-compat",
    },
    "cohere": {
        "base_url": "https://api.cohere.com/v1",
        "model": "rerank-multilingual-v3.0",
        "protocol": "cohere-compat",
    },
    "openai-compat": {
        "base_url": "",
        "model": "BAAI/bge-reranker-v2-m3",
        "protocol": "cohere-compat",
    },
}


def _resolve_config(cfg: "UserRerankerConfig | None") -> dict[str, Any] | None:
    """Reranker is opt-in: cfg=None means "no reranker", not "use env fallback"."""
    if cfg is None:
        return None
    return {
        "provider": cfg.provider,
        "base_url": cfg.base_url.rstrip("/") if cfg.base_url else "",
        "api_key": cfg.api_key,
        "model": cfg.model,
        "protocol": "cohere-compat",
    }


# ---------------------------------------------------------------------------
# Shared httpx client (mirrors embedding.py)
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=20.0)
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def rerank(
    query: str,
    documents: list[str],
    top_n: int,
    *,
    cfg: "UserRerankerConfig | None" = None,
) -> list[tuple[int, float]]:
    """Return [(original_idx, relevance_score), ...] sorted by score desc.

    Length <= top_n. When `cfg is None` or `documents` is empty, returns a
    passthrough preserving the first `top_n` indices with score=0.0 so callers
    can use a single code path.

    Score semantics: `relevance_score` is in the cross-encoder's native scale,
    typically [0, 1] but with very different distribution than cosine. Do NOT
    feed it back to LLM prompts calibrated on cosine — callers (KBSearchTool)
    keep the original cosine score on each hit and only use this function's
    output to REORDER the hits list.
    """
    resolved = _resolve_config(cfg)
    if resolved is None or not documents:
        return [(i, 0.0) for i in range(min(top_n, len(documents)))]
    if top_n <= 0:
        return []
    return await _rerank_cohere_compat(query, documents, top_n, resolved)


async def _rerank_cohere_compat(
    query: str, documents: list[str], top_n: int, resolved: dict[str, Any]
) -> list[tuple[int, float]]:
    """POST {base_url}/rerank with Cohere-shaped body.

    Request: {model, query, documents, top_n}
    Response: {results: [{index, relevance_score, ...}, ...]}
    """
    if not resolved["base_url"]:
        raise RuntimeError("reranker: base_url is empty")
    url = f"{resolved['base_url']}/rerank"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if resolved["api_key"]:
        headers["Authorization"] = f"Bearer {resolved['api_key']}"
    payload = {
        "model": resolved["model"],
        "query": query,
        "documents": documents,
        "top_n": min(top_n, len(documents)),
    }
    client = _get_client()
    resp = await client.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise RuntimeError("reranker: unexpected response shape (no `results` array)")
    out: list[tuple[int, float]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item["index"])
            score = float(item["relevance_score"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < len(documents):
            out.append((idx, score))
    return out
