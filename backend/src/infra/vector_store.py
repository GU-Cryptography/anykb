"""Vector store factory + adapters (Qdrant / Milvus Lite / Local SQLite).

This module is the single switch-point for "where do vectors live":
  VECTOR_STORE=qdrant  → QdrantStore  (local Qdrant, self-hosted server, or Qdrant Cloud)
  VECTOR_STORE=milvus  → MilvusStore  (Milvus Lite local .db, Standalone server, or Zilliz Cloud)
  VECTOR_STORE=local   → LocalVectorStore  (SQLite, offline, no network)

Adding another backend (Chroma, pgvector, …) is a single new class plus
one branch in get_store(). The RAG tool and ingest script call only the factory.

All stores implement the same minimal interface used by tools/restaurant_rag.py:

    async ensure_collection(vector_size: int) -> None
    async upsert(points: list[dict]) -> None
        points: [{"id": str, "vector": list[float], "payload": dict}, ...]
    async search(query_vector: list[float], city: str | None, limit: int) -> list[dict]
        returns: [{"id": str, "score": float, "vector": list[float], "payload": dict}, ...]

M2 additions (QdrantStore + MilvusStore):
    async create_collection(name, vector_size) -> None   (KB creation)
    async delete_collection(name) -> None                (KB deletion)
    async delete_by_filter(collection_name, filters)      (Document deletion)
    upsert / search now accept optional collection_name (None → self._collection).
"""
from __future__ import annotations

import asyncio
import math
from typing import Any, Protocol, runtime_checkable

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

from src.settings import get_settings


@runtime_checkable
class VectorStore(Protocol):
    async def ensure_collection(self, vector_size: int) -> None: ...
    async def upsert(self, points: list[dict[str, Any]]) -> None: ...
    async def search(
        self, query_vector: list[float], city: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Qdrant adapter — works for local (:6333), self-hosted server, and Qdrant Cloud.
# All three differ only by QDRANT_URL + QDRANT_API_KEY in .env.
# ---------------------------------------------------------------------------
class QdrantStore:
    def __init__(self) -> None:
        s = get_settings()
        self._client = AsyncQdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key or None)
        self._collection = s.qdrant_collection

    async def ensure_collection(
        self, vector_size: int, collection_name: str | None = None
    ) -> None:
        """Ensure the (default or named) collection exists with city payload index.

        Used by the legacy restaurant_rag path. For KB collections use
        create_collection() which is dedicated and doesn't add city indexing.
        """
        target = collection_name or self._collection
        collections = await self._client.get_collections()
        names = [c.name for c in collections.collections]
        if target not in names:
            await self._client.create_collection(
                collection_name=target,
                vectors_config=qmodels.VectorParams(
                    size=vector_size, distance=qmodels.Distance.COSINE
                ),
            )
            await self._client.create_payload_index(
                collection_name=target,
                field_name="city",
                field_schema=qmodels.PayloadSchemaType.KEYWORD,
            )
            return

        # Collection exists — guard against dim mismatch so we don't silently
        # write 1024-dim vectors into a 1536-dim collection (or vice versa).
        info = await self._client.get_collection(target)
        existing = info.config.params.vectors
        # qdrant returns VectorParams or dict[name -> VectorParams] depending on named vectors
        existing_size = (
            existing.size
            if isinstance(existing, qmodels.VectorParams)
            else next(iter(existing.values())).size
        )
        if existing_size != vector_size:
            raise RuntimeError(
                f"Qdrant collection '{target}' was created with vector_size="
                f"{existing_size} but current embedding model produces vector_size="
                f"{vector_size}. Drop the collection and re-ingest:\n"
                f"  curl -X DELETE {get_settings().qdrant_url}/collections/{target}"
            )

    async def create_collection(self, collection_name: str, vector_size: int) -> None:
        """Create a fresh KB collection with doc_id payload index for fast deletes.

        Idempotent: if it already exists with the right dim, returns silently.
        Mismatched dim raises (same logic as ensure_collection).
        """
        collections = await self._client.get_collections()
        names = [c.name for c in collections.collections]
        if collection_name in names:
            info = await self._client.get_collection(collection_name)
            existing = info.config.params.vectors
            existing_size = (
                existing.size
                if isinstance(existing, qmodels.VectorParams)
                else next(iter(existing.values())).size
            )
            if existing_size != vector_size:
                raise RuntimeError(
                    f"Qdrant collection '{collection_name}' exists with vector_size="
                    f"{existing_size}, but caller requested {vector_size}."
                )
            return

        await self._client.create_collection(
            collection_name=collection_name,
            vectors_config=qmodels.VectorParams(
                size=vector_size, distance=qmodels.Distance.COSINE
            ),
        )
        # doc_id index lets us efficiently delete all chunks for one document.
        await self._client.create_payload_index(
            collection_name=collection_name,
            field_name="doc_id",
            field_schema=qmodels.PayloadSchemaType.KEYWORD,
        )

    async def delete_collection(self, collection_name: str) -> None:
        """Drop a KB collection. Safe to call on a missing collection (no-op)."""
        collections = await self._client.get_collections()
        names = [c.name for c in collections.collections]
        if collection_name not in names:
            return
        await self._client.delete_collection(collection_name=collection_name)

    async def upsert(
        self,
        points: list[dict[str, Any]],
        collection_name: str | None = None,
    ) -> None:
        target = collection_name or self._collection
        pts = [
            qmodels.PointStruct(id=p["id"], vector=p["vector"], payload=p["payload"])
            for p in points
        ]
        await self._client.upsert(collection_name=target, points=pts)

    async def search(
        self,
        query_vector: list[float],
        city: str | None = None,
        limit: int = 10,
        collection_name: str | None = None,
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Vector search with optional payload filters.

        `city` is kept as syntactic sugar for the legacy restaurant flow and
        composes with `filters` (both, if given, must match — AND semantics).
        """
        target = collection_name or self._collection
        conditions: list[qmodels.FieldCondition] = []
        if city:
            conditions.append(
                qmodels.FieldCondition(key="city", match=qmodels.MatchValue(value=city))
            )
        if filters:
            for k, v in filters.items():
                conditions.append(
                    qmodels.FieldCondition(key=k, match=qmodels.MatchValue(value=v))
                )
        flt = qmodels.Filter(must=conditions) if conditions else None

        # query_points is the canonical search API in qdrant-client >= 1.10.
        # with_vectors=True is required for downstream MMR diversity reranking.
        res = await self._client.query_points(
            collection_name=target,
            query=query_vector,
            query_filter=flt,
            limit=limit,
            with_vectors=True,
            with_payload=True,
        )
        return [
            {
                "id": str(p.id),
                "score": float(p.score),
                "vector": list(p.vector) if p.vector else [],
                "payload": p.payload or {},
            }
            for p in res.points
        ]

    async def delete_by_filter(
        self, collection_name: str, filters: dict[str, str]
    ) -> None:
        """Delete all points in `collection_name` whose payload matches `filters`.

        Used to remove a single Document's chunks without dropping the whole KB.
        """
        if not filters:
            raise ValueError("delete_by_filter requires at least one filter")
        conds = [
            qmodels.FieldCondition(key=k, match=qmodels.MatchValue(value=v))
            for k, v in filters.items()
        ]
        await self._client.delete(
            collection_name=collection_name,
            points_selector=qmodels.FilterSelector(filter=qmodels.Filter(must=conds)),
        )


# ---------------------------------------------------------------------------
# Milvus adapter — Milvus Lite (embedded, local .db file) or Standalone server.
# Switch via VECTOR_STORE=milvus + MILVUS_URI=./data/milvus_local.db (Lite)
# or MILVUS_URI=http://host:19530 + MILVUS_TOKEN=... (Standalone / Zilliz).
#
# Differences vs Qdrant handled internally:
#   1. Milvus COSINE returns distance (0=match, 1=orthogonal). We convert to
#      similarity via `score = 1.0 - distance` so callers see Qdrant semantics.
#   2. pymilvus 3.0 search `output_fields=['*']` does NOT return dynamic fields,
#      so we list every known payload key explicitly (`_KNOWN_PAYLOAD_KEYS`).
#   3. `drop_collection` on Windows can hit WinError 183 (atomic-rename race in
#      milvus-lite 3.0); we fall back to physical `shutil.rmtree` of the
#      collection directory.
#   4. MilvusClient is sync; all calls go through `asyncio.to_thread` to match
#      the async store Protocol (same pattern as web_search.py uses for ddgs).
# ---------------------------------------------------------------------------
_KNOWN_PAYLOAD_KEYS = [
    # KB ingest (kb/ingest.py)
    "doc_id", "kb_id", "chunk_idx", "text", "filename",
    "source_type", "source_url",
    # Restaurant demo KB (data/ingest.py)
    "city", "cuisine", "name", "address", "rating",
    "description", "reason", "tags",
]


def _build_milvus_filter_expr(filters: dict[str, str] | None) -> str:
    """Convert {key: value} dict to Milvus filter expression syntax.

    {'city': '上海'} → 'city == "上海"'
    {'city': '上海', 'doc_id': 'abc'} → 'city == "上海" and doc_id == "abc"'

    Returns empty string if filters is None/empty (caller passes None to skip
    filter in Milvus search).
    """
    if not filters:
        return ""
    parts = []
    for k, v in filters.items():
        v_escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'{k} == "{v_escaped}"')
    return " and ".join(parts)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [0, 1] for normalized (or unit-length) embeddings,
    in [-1, 1] otherwise. Used in v3-M3 hybrid_search to recompute a per-chunk
    similarity score after RRF fusion, so the LLM-facing `score` stays in the
    same semantics the prompt was trained on (≥0.7 strong / 0.4-0.7 weak /
    <0.4 missing). Returns 0.0 if either side has zero magnitude.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class MilvusStore:
    def __init__(self) -> None:
        # pymilvus.settings runs `load_dotenv()` at module-import time and then
        # reads `MILVUS_URI` from os.environ to validate as HTTP-style URI. Our
        # MILVUS_URI setting carries a local .db file path (Milvus Lite) and
        # crashes that parser. Workaround:
        #   * Set MILVUS_URI="" in os.environ BEFORE importing pymilvus.
        #     load_dotenv() defaults to override=False so it won't overwrite our
        #     empty value with the .env file's path. pymilvus then sees an empty
        #     Config.MILVUS_URI and skips its default-connection parsing.
        #   * Pass our real uri straight to MilvusClient(uri=...) below, which
        #     accepts file paths directly.
        import os
        os.environ["MILVUS_URI"] = ""
        os.environ["MILVUS_CONN_ALIAS"] = "default"
        from pymilvus import MilvusClient

        # Windows compatibility patch (v3-M3): milvus-lite 3.0 uses
        # `os.rename(tmp, manifest.json)` in storage/manifest.py to atomically
        # commit collection metadata. POSIX rename allows overwriting; Windows
        # does NOT, so a manifest update with an existing target raises
        # WinError 183 ("Cannot create a file when that file already exists").
        # `os.replace` is the cross-platform overwrite-allowed primitive. We
        # patch the milvus_lite.storage.manifest module's rebound `os.rename`
        # to `os.replace` once per process — affects only milvus-lite internals,
        # not the user's own code.
        try:
            import milvus_lite.storage.manifest as _manifest_mod
            if getattr(_manifest_mod.os.rename, "__name__", "") != "replace":
                _manifest_mod.os.rename = os.replace  # type: ignore[assignment]
        except ImportError:
            pass  # milvus-lite not installed (Standalone server mode)

        s = get_settings()
        # Ensure parent dir exists for Lite mode (local .db path).
        if not s.milvus_uri.startswith("http"):
            from pathlib import Path
            Path(s.milvus_uri).parent.mkdir(parents=True, exist_ok=True)
        self._client = MilvusClient(
            uri=s.milvus_uri,
            token=s.milvus_token or None,
        )
        self._collection = s.qdrant_collection  # reuse default for travel demo
        self._uri = s.milvus_uri

    # ---- collection management ----

    def _has(self, name: str) -> bool:
        return bool(self._client.has_collection(collection_name=name))

    def _describe_dim(self, name: str) -> int:
        info = self._client.describe_collection(collection_name=name)
        for field in info.get("fields", []):
            if field.get("name") == "vector":
                return int(field.get("params", {}).get("dim", 0))
        return 0

    async def ensure_collection(
        self, vector_size: int, collection_name: str | None = None
    ) -> None:
        target = collection_name or self._collection
        await asyncio.to_thread(self._ensure_sync, target, vector_size)

    def _ensure_sync(self, name: str, vector_size: int) -> None:
        if self._has(name):
            existing = self._describe_dim(name)
            if existing and existing != vector_size:
                raise RuntimeError(
                    f"Milvus collection '{name}' was created with vector_size="
                    f"{existing} but current embedding model produces vector_size="
                    f"{vector_size}. Drop the collection and re-ingest."
                )
            return
        # v3-M3: explicit schema enabling hybrid search (dense COSINE +
        # server-side BM25 over `text`). The BM25 Function auto-derives the
        # sparse vectors from the text column at write time — no client-side
        # sparse embedding needed. Existing dense-only collections (legacy
        # `restaurants` travel KB and pre-v3-M3 user KBs) keep their
        # simple-mode schema; callers gate on `collection_supports_hybrid()`.
        from pymilvus import DataType, Function, FunctionType
        schema = self._client.create_schema(
            auto_id=False, enable_dynamic_field=True
        )
        schema.add_field(
            "id", DataType.VARCHAR, max_length=64, is_primary=True
        )
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=vector_size)
        schema.add_field(
            "text",
            DataType.VARCHAR,
            max_length=65535,
            enable_analyzer=True,
        )
        schema.add_field("text_bm25", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name="bm25_fn",
                input_field_names=["text"],
                output_field_names=["text_bm25"],
                function_type=FunctionType.BM25,
            )
        )

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )
        index_params.add_index(
            field_name="text_bm25",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
        )

        # milvus-lite 3.0 on Windows can hit WinError 183 (atomic-rename race)
        # during create_collection's two-index manifest flush. Retry once after
        # a short sleep — the race window is small (<100ms) and the second
        # rename almost always succeeds. If we get past creation, the schema
        # is durable; if we fail twice, surface the real error.
        import time
        for attempt in range(3):
            try:
                self._client.create_collection(
                    collection_name=name,
                    schema=schema,
                    index_params=index_params,
                )
                return
            except Exception as exc:  # noqa: BLE001
                if "183" not in str(exc) and "WinError" not in str(exc):
                    raise
                if attempt == 2:
                    raise
                # Clean half-created collection dir before retrying so the
                # second create_collection sees a fresh state.
                if not self._uri.startswith("http"):
                    import shutil
                    from pathlib import Path
                    col_dir = Path(self._uri) / "collections" / name
                    if col_dir.exists():
                        shutil.rmtree(col_dir, ignore_errors=True)
                time.sleep(0.2)

    async def create_collection(self, collection_name: str, vector_size: int) -> None:
        """Create a fresh KB collection. Idempotent on matching dim."""
        await asyncio.to_thread(self._ensure_sync, collection_name, vector_size)

    async def delete_collection(self, collection_name: str) -> None:
        await asyncio.to_thread(self._drop_sync, collection_name)

    def _drop_sync(self, name: str) -> None:
        if not self._has(name):
            return
        try:
            self._client.drop_collection(collection_name=name)
        except Exception as exc:  # noqa: BLE001
            # Windows WinError 183: milvus-lite 3.0 atomic-rename race during
            # close+flush. Fall back to physical removal of the collection dir.
            if "183" not in str(exc) and "WinError" not in str(exc):
                raise
            import shutil
            from pathlib import Path
            if not self._uri.startswith("http"):
                col_dir = Path(self._uri) / "collections" / name
                if col_dir.exists():
                    shutil.rmtree(col_dir, ignore_errors=True)

    # ---- data ops ----

    async def upsert(
        self,
        points: list[dict[str, Any]],
        collection_name: str | None = None,
    ) -> None:
        target = collection_name or self._collection
        # Flatten Qdrant-shape points {id, vector, payload} → Milvus rows
        # {id, vector, *payload_keys}. Unknown keys land in dynamic field.
        rows = []
        for p in points:
            row: dict[str, Any] = {
                "id": str(p["id"]),
                "vector": list(p["vector"]),
            }
            payload = p.get("payload") or {}
            for k, v in payload.items():
                row[k] = v
            rows.append(row)
        await asyncio.to_thread(
            self._client.upsert, collection_name=target, data=rows
        )

    async def search(
        self,
        query_vector: list[float],
        city: str | None = None,
        limit: int = 10,
        collection_name: str | None = None,
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        target = collection_name or self._collection

        # Compose filters: legacy `city` arg + explicit `filters` dict (AND).
        composed: dict[str, str] = {}
        if city:
            composed["city"] = city
        if filters:
            composed.update(filters)
        expr = _build_milvus_filter_expr(composed)

        # Explicit output fields — Milvus 3.0 ignores '*' for dynamic.
        output_fields = ["vector"] + _KNOWN_PAYLOAD_KEYS

        raw = await asyncio.to_thread(
            self._client.search,
            collection_name=target,
            data=[query_vector],
            limit=limit,
            filter=expr or None,
            output_fields=output_fields,
        )

        results: list[dict[str, Any]] = []
        if not raw or not raw[0]:
            return results
        for hit in raw[0]:
            entity = dict(hit.get("entity") or {})
            vec = entity.pop("vector", None) or []
            # Reconstruct payload from known keys (drop empty / null entries).
            payload = {
                k: v for k, v in entity.items()
                if k != "id" and v is not None and v != ""
            }
            # Milvus COSINE returns distance (0 = identical). Convert to
            # similarity so callers see Qdrant-equivalent scores in [0, 1].
            distance = float(hit.get("distance", 0.0))
            score = 1.0 - distance
            results.append({
                "id": str(hit.get("id", "")),
                "score": score,
                "vector": list(vec),
                "payload": payload,
            })
        return results

    async def delete_by_filter(
        self, collection_name: str, filters: dict[str, str]
    ) -> None:
        if not filters:
            raise ValueError("delete_by_filter requires at least one filter")
        expr = _build_milvus_filter_expr(filters)
        await asyncio.to_thread(
            self._client.delete, collection_name=collection_name, filter=expr
        )

    # ---- v3-M3: hybrid search (dense + BM25) + grouping ----

    async def collection_supports_hybrid(self, collection_name: str) -> bool:
        """True iff the collection was created with the v3-M3 hybrid schema
        (contains a `text_bm25` sparse vector field). Used by callers to gate
        between hybrid_search() and search() — old dense-only collections
        (legacy `restaurants`, pre-v3-M3 user KBs) keep working unchanged.
        """
        if not self._has(collection_name):
            return False
        info = await asyncio.to_thread(
            self._client.describe_collection, collection_name=collection_name
        )
        return any(
            f.get("name") == "text_bm25" for f in info.get("fields", [])
        )

    async def hybrid_search(
        self,
        query_vector: list[float],
        query_text: str,
        collection_name: str,
        limit: int = 10,
        filters: dict[str, str] | None = None,
        group_by: str | None = None,
    ) -> list[dict[str, Any]]:
        """Two-route hybrid search: dense vector + BM25, fused via RRF.

        Returns the same shape as `search()` so callers downstream see no
        difference. The `score` field is the raw dense COSINE similarity
        (recomputed client-side from the chunk's vector + query vector), NOT
        the RRF fused rank — this preserves the v2-M6 prompt's 3-tier
        threshold judgment (>=0.7 strong / 0.4-0.7 weak / <0.4 missing) and
        keeps similarity numbers intuitive to users in citations.

        RRF affects ONLY which chunks appear in the top-N (improves recall by
        letting BM25 keyword hits surface alongside semantic neighbors); it
        does not affect the per-chunk similarity number we return.

        `group_by="doc_id"` enables Milvus grouping_search so each document
        contributes at most one chunk to top-N — avoids one long doc occupying
        all top-k slots.
        """
        from pymilvus import AnnSearchRequest, RRFRanker

        expr = _build_milvus_filter_expr(filters) if filters else ""

        # Over-fetch each route so RRF has more candidates to fuse.
        per_route_limit = max(limit * 2, 10)
        dense_req = AnnSearchRequest(
            data=[query_vector],
            anns_field="vector",
            param={"metric_type": "COSINE"},
            limit=per_route_limit,
            expr=expr or None,
        )
        sparse_req = AnnSearchRequest(
            data=[query_text],
            anns_field="text_bm25",
            param={"metric_type": "BM25"},
            limit=per_route_limit,
            expr=expr or None,
        )

        kwargs: dict[str, Any] = dict(
            collection_name=collection_name,
            reqs=[dense_req, sparse_req],
            ranker=RRFRanker(k=60),
            limit=limit,
            output_fields=["vector"] + _KNOWN_PAYLOAD_KEYS,
        )
        if group_by:
            kwargs["group_by_field"] = group_by

        raw = await asyncio.to_thread(self._client.hybrid_search, **kwargs)

        results: list[dict[str, Any]] = []
        if not raw or not raw[0]:
            return results
        for hit in raw[0]:
            entity = dict(hit.get("entity") or {})
            vec = entity.pop("vector", None) or []
            payload = {
                k: v for k, v in entity.items()
                if k != "id" and v is not None and v != ""
            }
            # Score = raw dense cosine similarity (NOT the RRF fused score).
            # We have query_vector and the chunk vector via output_fields, so
            # recompute cosine here. Keeps Qdrant-equivalent semantics for
            # the LLM-facing score field.
            cosine_sim = _cosine_similarity(query_vector, vec) if vec else 0.0
            results.append({
                "id": str(hit.get("id", "")),
                "score": cosine_sim,
                "vector": list(vec),
                "payload": payload,
            })
        return results


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_store: VectorStore | None = None


def get_store() -> VectorStore:
    """Return the configured vector store singleton."""
    global _store
    if _store is not None:
        return _store

    s = get_settings()
    backend = (s.vector_store or "qdrant").lower()

    if backend == "qdrant":
        _store = QdrantStore()
    elif backend == "milvus":
        _store = MilvusStore()
    elif backend == "local":
        # Lazy import — avoid SQLite cost when not used.
        from src.infra.local_vector import LocalVectorStore

        _store = LocalVectorStore(db_path=s.local_vector_db_path)
    else:
        raise ValueError(
            f"Unknown VECTOR_STORE='{backend}'. Supported: 'qdrant', 'milvus', 'local'."
        )
    return _store


def reset_store() -> None:
    """Test helper: clear the cached singleton so the next get_store() rebuilds."""
    global _store
    _store = None
