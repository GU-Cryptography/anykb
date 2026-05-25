"""Smoke test for MilvusStore against a temp Milvus Lite .db file.

Skipped when `pymilvus` is not installed (Qdrant-only deployments).

Patches get_settings() directly instead of setting MILVUS_URI env var: pymilvus'
module-level Config.MILVUS_URI validator chokes on local file paths (expects
HTTP form). Our production code handles this by popping the env var inside
MilvusStore.__init__, but tests should not rely on that side effect.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("pymilvus")
pytest.importorskip("milvus_lite")


@pytest.mark.asyncio
async def test_milvus_store_full_lifecycle(monkeypatch):
    tmp_dir = Path(tempfile.mkdtemp(prefix="milvus_smoke_"))
    db_path = tmp_dir / "test.db"

    try:
        from src import settings as settings_mod
        from src.infra import vector_store as vs

        real_get_settings = settings_mod.get_settings.__wrapped__

        def patched():
            s = real_get_settings()
            s.vector_store = "milvus"
            s.milvus_uri = str(db_path)
            s.milvus_token = ""
            return s

        settings_mod.get_settings.cache_clear()
        monkeypatch.setattr(settings_mod, "get_settings", patched)
        monkeypatch.setattr(vs, "get_settings", patched)
        vs.reset_store()

        store = vs.get_store()
        assert type(store).__name__ == "MilvusStore"

        collection = "kb_smoke_test"

        # create + idempotent re-create
        await store.create_collection(collection, vector_size=4)
        await store.create_collection(collection, vector_size=4)

        # v3-M3: new collections get the hybrid schema (text_bm25 sparse field).
        assert await store.collection_supports_hybrid(collection), (
            "v3-M3 schema should expose text_bm25 sparse field"
        )

        # upsert 3 chunks across 2 docs
        await store.upsert(
            [
                {
                    "id": "doc1-chunk0",
                    "vector": [1.0, 0.0, 0.0, 0.0],
                    "payload": {
                        "doc_id": "doc1", "kb_id": "kb-smoke", "chunk_idx": 0,
                        "text": "苹果是水果", "filename": "fruits.md",
                        "source_type": "file", "source_url": "",
                    },
                },
                {
                    "id": "doc1-chunk1",
                    "vector": [0.0, 1.0, 0.0, 0.0],
                    "payload": {
                        "doc_id": "doc1", "kb_id": "kb-smoke", "chunk_idx": 1,
                        "text": "香蕉黄色甜的", "filename": "fruits.md",
                        "source_type": "file", "source_url": "",
                    },
                },
                {
                    "id": "doc2-chunk0",
                    "vector": [0.0, 0.0, 1.0, 0.0],
                    "payload": {
                        "doc_id": "doc2", "kb_id": "kb-smoke", "chunk_idx": 0,
                        "text": "Python 是一门语言", "filename": "tech.md",
                        "source_type": "file", "source_url": "",
                    },
                },
            ],
            collection_name=collection,
        )

        # search — identity match scores ~1.0 and roundtrips payload incl. CJK
        hits = await store.search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            limit=3,
            collection_name=collection,
        )
        assert len(hits) == 3
        assert hits[0]["id"] == "doc1-chunk0"
        assert hits[0]["score"] > 0.99
        assert hits[0]["payload"]["text"] == "苹果是水果"
        assert hits[0]["vector"], "vector returned for MMR rerank"

        # search with filter — only doc1 chunks come back
        filtered = await store.search(
            query_vector=[0.5, 0.5, 0.0, 0.0],
            limit=10,
            collection_name=collection,
            filters={"doc_id": "doc1"},
        )
        assert len(filtered) == 2
        assert all(h["payload"]["doc_id"] == "doc1" for h in filtered)

        # delete by filter — doc1 gone, doc2 stays
        await store.delete_by_filter(collection, {"doc_id": "doc1"})
        await asyncio.sleep(0.5)  # let Milvus Lite WAL flush
        after = await store.search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            limit=10,
            collection_name=collection,
        )
        assert len(after) == 1
        assert after[0]["payload"]["doc_id"] == "doc2"

        # drop collection (idempotent)
        await store.delete_collection(collection)
        await store.delete_collection(collection)

    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        from src.infra import vector_store as vs
        vs.reset_store()


@pytest.mark.asyncio
async def test_milvus_hybrid_search_lifecycle(monkeypatch):
    """v3-M3: dense + BM25 hybrid retrieval with RRF fusion + grouping.

    Sets up 3 chunks across 2 docs with deliberately overlapping semantics but
    distinct keywords (`Redis Stream` vs `RabbitMQ` vs `Kafka`). A query for
    "Redis Stream" should rank the literal-keyword chunk first because BM25
    catches the exact term even though dense similarity might prefer a more
    "general MQ" chunk in some embedding spaces. Grouping reduces 3 hits to 2
    (one per doc).
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="milvus_hybrid_"))
    db_path = tmp_dir / "test.db"

    try:
        from src import settings as settings_mod
        from src.infra import vector_store as vs

        real_get_settings = settings_mod.get_settings.__wrapped__

        def patched():
            s = real_get_settings()
            s.vector_store = "milvus"
            s.milvus_uri = str(db_path)
            s.milvus_token = ""
            return s

        settings_mod.get_settings.cache_clear()
        monkeypatch.setattr(settings_mod, "get_settings", patched)
        monkeypatch.setattr(vs, "get_settings", patched)
        vs.reset_store()

        store = vs.get_store()
        collection = "kb_hybrid_test"

        # Identity-axis vectors so cosine sim is interpretable in assertions.
        await store.create_collection(collection, vector_size=4)
        assert await store.collection_supports_hybrid(collection)

        await store.upsert(
            [
                {
                    "id": "doc1-chunk0",
                    "vector": [1.0, 0.0, 0.0, 0.0],
                    "payload": {
                        "doc_id": "doc1", "kb_id": "kb-hybrid", "chunk_idx": 0,
                        "text": "Redis Stream is a log-style message queue",
                        "filename": "mq.md",
                        "source_type": "file", "source_url": "",
                    },
                },
                {
                    "id": "doc1-chunk1",
                    "vector": [0.9, 0.1, 0.0, 0.0],
                    "payload": {
                        "doc_id": "doc1", "kb_id": "kb-hybrid", "chunk_idx": 1,
                        "text": "RabbitMQ uses AMQP and supports queues",
                        "filename": "mq.md",
                        "source_type": "file", "source_url": "",
                    },
                },
                {
                    "id": "doc2-chunk0",
                    "vector": [0.0, 1.0, 0.0, 0.0],
                    "payload": {
                        "doc_id": "doc2", "kb_id": "kb-hybrid", "chunk_idx": 0,
                        "text": "Kafka is a distributed event streaming platform",
                        "filename": "kafka.md",
                        "source_type": "file", "source_url": "",
                    },
                },
            ],
            collection_name=collection,
        )
        await asyncio.sleep(0.5)  # let BM25 sparse generation + WAL flush

        # Hybrid search — query "Redis Stream" with dense vector close to doc1-chunk0.
        hits = await store.hybrid_search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            query_text="Redis Stream",
            collection_name=collection,
            limit=3,
        )
        assert len(hits) >= 1, "hybrid should return at least one hit"
        # BM25 + dense fusion: the exact-keyword chunk (doc1-chunk0) should win.
        assert hits[0]["id"] == "doc1-chunk0", (
            f"hybrid top hit should be the Redis Stream chunk, got {hits[0]['id']}"
        )
        # Score is cosine sim (not RRF). Query is identity to doc1-chunk0 vector.
        assert 0.99 <= hits[0]["score"] <= 1.01, (
            f"score should be cosine ~1.0 for identity vector match, got {hits[0]['score']}"
        )
        # Payload + vector roundtrip intact (vector needed for MMR rerank).
        assert hits[0]["payload"]["filename"] == "mq.md"
        assert hits[0]["vector"], "vector field must be returned for MMR"

        # Grouping: same query but group_by=doc_id → 1 chunk per doc, so at
        # most 2 hits across doc1+doc2.
        grouped = await store.hybrid_search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            query_text="message queue",
            collection_name=collection,
            limit=10,
            group_by="doc_id",
        )
        doc_ids = [h["payload"]["doc_id"] for h in grouped]
        assert len(grouped) <= 2, f"grouping should cap at 2 docs, got {len(grouped)}"
        assert len(set(doc_ids)) == len(doc_ids), (
            f"grouping should yield unique doc_ids, got {doc_ids}"
        )

        await store.delete_collection(collection)

    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        from src.infra import vector_store as vs
        vs.reset_store()


@pytest.mark.asyncio
async def test_milvus_collection_supports_hybrid_false_for_missing(monkeypatch):
    """Empty (non-existent) collection name returns False, not error."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="milvus_hybrid_neg_"))
    db_path = tmp_dir / "test.db"
    try:
        from src import settings as settings_mod
        from src.infra import vector_store as vs

        real_get_settings = settings_mod.get_settings.__wrapped__

        def patched():
            s = real_get_settings()
            s.vector_store = "milvus"
            s.milvus_uri = str(db_path)
            s.milvus_token = ""
            return s

        settings_mod.get_settings.cache_clear()
        monkeypatch.setattr(settings_mod, "get_settings", patched)
        monkeypatch.setattr(vs, "get_settings", patched)
        vs.reset_store()

        store = vs.get_store()
        assert not await store.collection_supports_hybrid("definitely_not_a_collection")
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        from src.infra import vector_store as vs
        vs.reset_store()
