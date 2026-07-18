"""Long-term memory vector index (v3-M3 memory-optimization).

A thin, degradation-first wrapper over the shared vector-store abstraction
(``infra/vector_store.py:get_store``) dedicated to the single
``user_memory_vectors`` collection (PRD §5.2). Every user's memory vectors live
in this one collection, scalar-filtered by ``user_id`` (Milvus "partition key"
semantics are approximated with a normal scalar filter — portable across
Qdrant/Milvus).

Why a wrapper and not calls scattered at the memory call sites: L2 recall and
the write path must both treat this index as *optional infrastructure* (see
`.trellis/spec/backend/error-handling.md`). This module is the one place that:

  * gates on backend capability — the ``local`` SQLite store is single-collection
    and restaurant-shaped, so it has no ``create_collection`` / ``delete_by_filter``;
    we detect that (``hasattr``, exactly like ``kb/ingest.py``) and no-op, letting
    long-term memory fall back to the durable PG rows (importance-ranked L2).
  * swallows every store error (log once, return the degraded value) so a
    Milvus/Qdrant outage can never 500 a chat turn or lose a memory row — the PG
    row is always written first by the caller; a missing vector is acceptable.

Collection shape (mirrors the KB collection creation in ``kb/ingest.py``):
  pk      = memory_id            (the UserMemory.id)
  vector  = embedding            (dim = get_vector_size(cfg) at first write)
  payload = user_id, memory_type, importance, memory_id, text
            (memory_id duplicated into the payload so single-row deletion works
             uniformly through delete_by_filter on BOTH backends — Qdrant filters
             match payload keys, not the point id.)

``text`` (the memory content) is NOT decorative: ``MilvusStore.create_collection``
builds the v3-M3 hybrid schema with a **required** ``text`` field that feeds a
server-side BM25 function (see ``infra/vector_store.py:_ensure_sync``), exactly
like ``kb/ingest.py`` chunks. Omitting it makes every Milvus upsert fail the
required-field check and silently drop long-term memory to the PG fallback on
the production backend. On Qdrant/local it's just an extra payload key.
"""
from __future__ import annotations

import logging
from typing import Any

from src.infra.vector_store import get_store

log = logging.getLogger(__name__)

# One shared collection for all users (PRD §5.2). Scalar-filtered by user_id.
MEMORY_COLLECTION = "user_memory_vectors"


def _multi_collection(store: Any) -> bool:
    """True when the active vector backend supports named collections.

    Qdrant / Milvus expose ``create_collection`` + ``delete_by_filter``; the
    ``local`` SQLite store does not. Same capability probe ``kb/ingest.py`` uses
    to gate KB features — keeps long-term memory vectors on exactly the backends
    KB vectors already run on, and degrades to PG-only elsewhere.
    """
    return hasattr(store, "create_collection") and hasattr(store, "delete_by_filter")


def vector_capability_available() -> bool:
    """True when the active backend supports the memory-vector collection ops.

    A cheap up-front probe so the v3-M5 maintenance dedup can skip its whole
    embed-then-search loop on the ``local`` backend (where every search would
    return ``[]`` anyway) instead of billing the user's embedding key per row for
    a guaranteed-empty ANN query. Mirrors the ``_multi_collection`` gate the
    write / read helpers already apply per call.
    """
    return _multi_collection(get_store())


async def upsert_memory_vector(
    memory_id: str,
    vector: list[float],
    content: str,
    user_id: str,
    memory_type: str,
    importance: float,
) -> bool:
    """Index one memory's embedding. Best-effort: returns True on success, False
    on any degrade (unsupported backend / store error). Never raises.

    ``content`` is stored as the payload ``text`` — required by the Milvus
    hybrid-search schema's BM25 function (mirrors ``kb/ingest.py``); harmless
    extra payload on Qdrant/local.

    The collection is created lazily on first write with the incoming vector's
    dim (idempotent — ``create_collection`` short-circuits when it already
    exists at a matching dim; a dim mismatch raises inside the store and is
    swallowed here as a degrade, leaving the durable PG row untouched).
    """
    if not vector:
        return False
    store = get_store()
    if not _multi_collection(store):
        return False
    try:
        await store.create_collection(MEMORY_COLLECTION, len(vector))
        await store.upsert(
            [
                {
                    "id": memory_id,
                    "vector": list(vector),
                    "payload": {
                        "user_id": user_id,
                        "memory_type": memory_type,
                        "importance": float(importance),
                        "memory_id": memory_id,
                        "text": content or "",
                    },
                }
            ],
            collection_name=MEMORY_COLLECTION,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — vector index is optional infra.
        log.warning("memory_vector_upsert_failed memory=%s error=%s", memory_id, exc)
        return False


async def search_memory_vectors(
    vector: list[float], user_id: str, limit: int = 3
) -> list[str]:
    """ANN search this user's memory vectors → ordered memory ids (top-k first).

    Returns [] on any degrade (unsupported backend, cold/missing collection,
    store error) so the caller falls back to the PG importance ranking. Never
    raises.
    """
    if not vector:
        return []
    store = get_store()
    if not _multi_collection(store):
        return []
    try:
        hits = await store.search(
            vector,
            collection_name=MEMORY_COLLECTION,
            limit=limit,
            filters={"user_id": user_id},
        )
    except Exception as exc:  # noqa: BLE001 — degrade to PG fallback, never break chat.
        log.warning("memory_vector_search_failed user=%s error=%s", user_id, exc)
        return []
    out: list[str] = []
    for h in hits or []:
        # Prefer the payload memory_id (portable), fall back to the point id.
        payload = h.get("payload") or {}
        mid = payload.get("memory_id") or h.get("id")
        if mid:
            out.append(str(mid))
    return out


async def search_memory_vectors_scored(
    vector: list[float], user_id: str, limit: int = 50
) -> list[tuple[str, float]]:
    """ANN search this user's memory vectors → ``[(memory_id, score), ...]``.

    The score-carrying sibling of :func:`search_memory_vectors`, used by the
    v3-M5 maintenance dedup to threshold near-duplicate neighbors (cosine >
    0.85). The underlying store already returns a Qdrant-equivalent similarity
    ``score`` in ``[0, 1]`` (``vector_store.py`` normalizes Milvus distance), so
    this is the minimal faithful extension the task calls for — same isolation
    (``user_id`` filter) and same degradation contract: ``[]`` on any degrade
    (unsupported backend / cold collection / store error), never raises.
    """
    if not vector:
        return []
    store = get_store()
    if not _multi_collection(store):
        return []
    try:
        hits = await store.search(
            vector,
            collection_name=MEMORY_COLLECTION,
            limit=limit,
            filters={"user_id": user_id},
        )
    except Exception as exc:  # noqa: BLE001 — degrade to no-dedup, never break maintenance.
        log.warning("memory_vector_search_scored_failed user=%s error=%s", user_id, exc)
        return []
    out: list[tuple[str, float]] = []
    for h in hits or []:
        payload = h.get("payload") or {}
        mid = payload.get("memory_id") or h.get("id")
        score = h.get("score")
        if mid and score is not None:
            out.append((str(mid), float(score)))
    return out


async def delete_memory_vectors_by_user(user_id: str) -> None:
    """Drop all of a user's memory vectors (purge_user chain, PRD §5.7).

    Best-effort: unsupported backend or store error is logged and skipped so the
    rest of the user-purge continues. Never raises.
    """
    store = get_store()
    if not _multi_collection(store):
        return
    try:
        await store.delete_by_filter(MEMORY_COLLECTION, {"user_id": user_id})
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort.
        log.warning("memory_vector_purge_failed user=%s error=%s", user_id, exc)


async def delete_memory_vector(memory_id: str) -> None:
    """Drop a single memory's vector (reserved for M4's DELETE /api/memories/{id}).

    Filters on the payload ``memory_id`` (not the point id) so it works the same
    on Qdrant and Milvus. Best-effort; never raises.
    """
    store = get_store()
    if not _multi_collection(store):
        return
    try:
        await store.delete_by_filter(MEMORY_COLLECTION, {"memory_id": memory_id})
    except Exception as exc:  # noqa: BLE001 — best-effort.
        log.warning("memory_vector_delete_failed memory=%s error=%s", memory_id, exc)
