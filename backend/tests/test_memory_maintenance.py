"""Tests for v3-M5 memory maintenance: dedup / decay / eviction + 24h scan.

All external services are faked (no real Redis / Milvus / LLM / clock):
  * an in-memory ``_FakeVectorStore`` (Qdrant/Milvus-shaped, returns scores),
  * a monkeypatched embed returning a constant vector,
  * a stub ``extract_conversation_memories`` so the scan asserts wiring only,
  * time driven by seeded ``created_at`` / ``last_*`` columns — never a real sleep.

DB-backed via the shared ``db`` fixture (temp SQLite; ``init_db`` builds
``user_memories`` incl. the M5 ``deleted_at`` / ``last_decayed_at`` columns).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest


# --------------------------------------------------------------------------- #
# Hygiene: reset Redis singleton, settings cache, the maintenance task ref, and
# any leaked long-term-memory background tasks around every test.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_globals():
    from src.infra.redis_client import reset_redis
    from src.settings import get_settings

    reset_redis()
    get_settings.cache_clear()
    yield
    import src.conversations.memory_maintenance as mm

    mm._maintenance_task = None
    reset_redis()
    get_settings.cache_clear()


def _embed_cfg():
    from src.settings_user import UserEmbeddingConfig

    return UserEmbeddingConfig(
        provider="openai-compat", base_url="https://e.example.com",
        api_key="k", model="bge-m3", dim=4,
    )


def _cos(a, b):
    import math

    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9)


class _FakeVectorStore:
    """In-memory multi-collection store (Qdrant/Milvus shape) returning scores."""

    def __init__(self, *, fail: bool = False):
        self.points: dict[str, tuple[list, dict]] = {}
        self.fail = fail
        self.deleted_filters: list[dict] = []

    async def create_collection(self, name, vector_size):
        if self.fail:
            raise RuntimeError("boom-create")

    async def upsert(self, points, collection_name=None):
        for p in points:
            self.points[p["id"]] = (list(p["vector"]), dict(p.get("payload") or {}))

    async def search(self, query_vector, city=None, limit=10, collection_name=None, filters=None):
        if self.fail:
            raise RuntimeError("boom-search")
        rows = []
        for pid, (vec, payload) in self.points.items():
            if filters and any(payload.get(k) != v for k, v in filters.items()):
                continue
            rows.append({"id": pid, "score": _cos(query_vector, vec), "vector": vec, "payload": payload})
        rows.sort(key=lambda x: x["score"], reverse=True)
        return rows[:limit]

    async def delete_by_filter(self, collection_name, filters):
        if self.fail:
            raise RuntimeError("boom-delete")
        self.deleted_filters.append(dict(filters))
        doomed = [
            pid for pid, (_v, pl) in self.points.items()
            if all(pl.get(k) == v for k, v in filters.items())
        ]
        for pid in doomed:
            self.points.pop(pid, None)


class _LocalLikeStore:
    """Single-collection store (no create_collection) → capability probe False."""

    async def search(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("dedup must not search on an unsupported backend")


def _now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# DB seed helper — full control over the maintenance-relevant columns.
# --------------------------------------------------------------------------- #
async def _seed(
    db, user_id, *, mtype="fact", content="内容", importance=0.5,
    created_at=None, last_accessed_at=None, last_decayed_at=None,
    deleted_at=None, mem_id=None,
) -> str:
    from src.conversations.models import UserMemory

    mid = mem_id or str(uuid.uuid4())
    async with db.get_session_factory()() as s:
        s.add(UserMemory(
            id=mid, user_id=user_id, memory_type=mtype, content=content,
            importance=importance, created_at=created_at or _now(),
            last_accessed_at=last_accessed_at, last_decayed_at=last_decayed_at,
            deleted_at=deleted_at,
        ))
        await s.commit()
    return mid


async def _get(db, mem_id):
    from src.conversations.models import UserMemory

    async with db.get_session_factory()() as s:
        return await s.get(UserMemory, mem_id)


async def _seed_conv(db, user_id, *, updated_at, finalized=False) -> str:
    from src.conversations.models import Conversation, Message

    cid = str(uuid.uuid4())
    async with db.get_session_factory()() as s:
        s.add(Conversation(
            id=cid, user_id=user_id, title="t", kb_id=None,
            updated_at=updated_at,
            finalized_at=_now() if finalized else None,
        ))
        s.add(Message(id=f"{cid}-0", conversation_id=cid, role="user",
                      content="我们用 K8s", created_at=updated_at))
        await s.commit()
    return cid


# =========================================================================== #
# Dedup — vector-only; keep importance-highest, soft-delete + vector-delete rest
# =========================================================================== #
@pytest.mark.asyncio
async def test_dedup_keeps_highest_importance_soft_deletes_rest(db, monkeypatch):
    import src.conversations.long_term_memory as ltm
    import src.conversations.memory_maintenance as mm
    import src.infra.memory_vector as mv

    store = _FakeVectorStore()
    monkeypatch.setattr(mv, "get_store", lambda: store)

    async def _cfg(uid):
        return _embed_cfg()

    monkeypatch.setattr(ltm, "_resolve_user_embedding_by_id", _cfg)

    async def _const_embed(content, cfg):
        return [1.0, 0.0, 0.0, 0.0]   # identical → cosine 1.0 for all three

    monkeypatch.setattr(mm, "_embed_memory", _const_embed)

    hi = await _seed(db, "u1", content="A", importance=0.9)
    mid = await _seed(db, "u1", content="B", importance=0.7)
    lo = await _seed(db, "u1", content="C", importance=0.5)
    for m in (hi, mid, lo):
        await store.upsert(
            [{"id": m, "vector": [1.0, 0.0, 0.0, 0.0],
              "payload": {"user_id": "u1", "memory_id": m}}],
            collection_name="user_memory_vectors",
        )

    counters = await mm.maintain_user_memories("u1")
    assert counters["deduped"] == 2

    # Highest-importance survives; the two near-duplicates are soft-deleted.
    assert (await _get(db, hi)).deleted_at is None
    assert (await _get(db, mid)).deleted_at is not None
    assert (await _get(db, lo)).deleted_at is not None
    # Losers' vectors hard-deleted; winner's vector kept.
    assert mid not in store.points and lo not in store.points
    assert hi in store.points
    assert {"memory_id": mid} in store.deleted_filters
    assert {"memory_id": lo} in store.deleted_filters


@pytest.mark.asyncio
async def test_dedup_skipped_without_embedding(db, monkeypatch):
    import src.conversations.long_term_memory as ltm
    import src.conversations.memory_maintenance as mm
    import src.infra.memory_vector as mv

    store = _FakeVectorStore()
    monkeypatch.setattr(mv, "get_store", lambda: store)

    async def _none(uid):    # user has no embedding cfg → dedup degrades to skip
        return None

    monkeypatch.setattr(ltm, "_resolve_user_embedding_by_id", _none)

    async def _boom_embed(content, cfg):  # must not embed when skipping
        raise AssertionError("dedup must not embed without an embedding cfg")

    monkeypatch.setattr(mm, "_embed_memory", _boom_embed)

    a = await _seed(db, "u1", content="A", importance=0.9)
    b = await _seed(db, "u1", content="A", importance=0.5)
    assert await mm._dedup_user_memories("u1") == 0
    assert (await _get(db, a)).deleted_at is None
    assert (await _get(db, b)).deleted_at is None


@pytest.mark.asyncio
async def test_dedup_skipped_without_vector_capability(db, monkeypatch):
    import src.conversations.long_term_memory as ltm
    import src.conversations.memory_maintenance as mm
    import src.infra.memory_vector as mv

    monkeypatch.setattr(mv, "get_store", lambda: _LocalLikeStore())

    async def _cfg(uid):     # embedding present, but the backend can't do collections
        return _embed_cfg()

    monkeypatch.setattr(ltm, "_resolve_user_embedding_by_id", _cfg)

    await _seed(db, "u1", content="A", importance=0.9)
    await _seed(db, "u1", content="A", importance=0.5)
    assert await mm._dedup_user_memories("u1") == 0


# =========================================================================== #
# Decay — 30d-stale *= 0.9, idempotency gate, explicit exempt, recent exempt
# =========================================================================== #
@pytest.mark.asyncio
async def test_decay_stale_once_and_idempotent_double_run(db, monkeypatch):
    import src.conversations.memory_maintenance as mm

    old = _now() - timedelta(days=40)
    m = await _seed(db, "u1", importance=0.8, created_at=old, last_accessed_at=None)

    assert await mm._decay_user_memories("u1") == 1
    row = await _get(db, m)
    assert row.importance == pytest.approx(0.72)      # 0.8 * 0.9
    assert row.last_decayed_at is not None

    # Second run the same night (or a second instance) → the last_decayed_at gate
    # blocks a re-decay: no rows match, importance unchanged.
    assert await mm._decay_user_memories("u1") == 0
    assert (await _get(db, m)).importance == pytest.approx(0.72)


@pytest.mark.asyncio
async def test_decay_exempts_explicit_and_recently_accessed(db, monkeypatch):
    import src.conversations.memory_maintenance as mm

    old = _now() - timedelta(days=40)
    # explicit: user asked to remember → never auto-decays even when stale.
    expl = await _seed(db, "u1", mtype="explicit", importance=0.8, created_at=old)
    # accessed yesterday → not "stale" (< 30d) → not decayed.
    fresh = await _seed(db, "u1", importance=0.8, created_at=old,
                        last_accessed_at=_now() - timedelta(days=1))
    # a genuinely stale row so the UPDATE isn't a no-op for the wrong reason.
    stale = await _seed(db, "u1", importance=0.8, created_at=old, last_accessed_at=None)

    assert await mm._decay_user_memories("u1") == 1   # only `stale`
    assert (await _get(db, expl)).importance == pytest.approx(0.8)
    assert (await _get(db, fresh)).importance == pytest.approx(0.8)
    assert (await _get(db, stale)).importance == pytest.approx(0.72)


# =========================================================================== #
# Eviction — importance < 0.3 soft-deleted + vector-deleted; explicit exempt
# =========================================================================== #
@pytest.mark.asyncio
async def test_evict_low_importance_and_exempts_explicit(db, monkeypatch):
    import src.conversations.memory_maintenance as mm
    import src.infra.memory_vector as mv

    store = _FakeVectorStore()
    monkeypatch.setattr(mv, "get_store", lambda: store)

    doomed = await _seed(db, "u1", importance=0.2, content="低价值")
    expl = await _seed(db, "u1", mtype="explicit", importance=0.2, content="显式低价值")
    keep = await _seed(db, "u1", importance=0.5, content="够重要")
    for m in (doomed, expl, keep):
        await store.upsert(
            [{"id": m, "vector": [1.0], "payload": {"user_id": "u1", "memory_id": m}}],
            collection_name="user_memory_vectors",
        )

    assert await mm._evict_user_memories("u1") == 1

    assert (await _get(db, doomed)).deleted_at is not None
    assert doomed not in store.points                # vector hard-deleted
    assert (await _get(db, expl)).deleted_at is None  # explicit exempt
    assert (await _get(db, keep)).deleted_at is None  # above the floor
    assert expl in store.points and keep in store.points


# =========================================================================== #
# Soft-delete propagation — hidden from L2 recall + L1 profile aggregation
# =========================================================================== #
@pytest.mark.asyncio
async def test_soft_deleted_excluded_from_recall_and_profile(db, monkeypatch):
    import src.conversations.long_term_memory as ltm

    async def _none(uid):    # no embedding → L2 uses the PG importance fallback
        return None

    monkeypatch.setattr(ltm, "_resolve_user_embedding_by_id", _none)

    # A higher-importance row that is soft-deleted must NOT outrank the live one.
    live = await _seed(db, "u1", mtype="fact", content="活着", importance=0.5)
    await _seed(db, "u1", mtype="fact", content="死了", importance=0.9,
                deleted_at=_now())

    res = await ltm.retrieve_long_term_memories("u1", "查询", limit=5)
    assert [m["content"] for m in res] == ["活着"]
    assert all(m["id"] == live for m in res)

    # Profile aggregation likewise ignores the soft-deleted (higher-importance) role.
    await _seed(db, "u1", mtype="profile", content="活角色", importance=0.5)
    await _seed(db, "u1", mtype="profile", content="死角色", importance=0.9,
                deleted_at=_now())
    profile = await ltm.get_user_profile("u1")
    assert profile["role"] == "活角色"


# =========================================================================== #
# 24h scan — CAS claim + extract once; concurrency; already-finalized; LIMIT
# =========================================================================== #
@pytest.mark.asyncio
async def test_scan_finalizes_and_extracts_stale_conversation(db, monkeypatch):
    import src.conversations.long_term_memory as ltm
    import src.conversations.memory_maintenance as mm
    from src.conversations.models import Conversation

    calls: list[tuple] = []

    async def _fake_extract(cid, uid, llm_cfg=None):
        calls.append((cid, uid))
        return 2

    monkeypatch.setattr(ltm, "extract_conversation_memories", _fake_extract)

    stale = await _seed_conv(db, "u1", updated_at=_now() - timedelta(hours=25))
    fresh = await _seed_conv(db, "u1", updated_at=_now() - timedelta(hours=1))

    assert await mm.scan_stale_conversations() == 1
    assert calls == [(stale, "u1")]              # only the 25h-idle one
    async with db.get_session_factory()() as s:
        assert (await s.get(Conversation, stale)).finalized_at is not None
        assert (await s.get(Conversation, fresh)).finalized_at is None


@pytest.mark.asyncio
async def test_scan_skips_already_finalized(db, monkeypatch):
    import src.conversations.long_term_memory as ltm
    import src.conversations.memory_maintenance as mm

    monkeypatch.setattr(
        ltm, "extract_conversation_memories",
        lambda *a, **k: pytest.fail("must not extract an already-finalized conv"),
    )
    await _seed_conv(db, "u1", updated_at=_now() - timedelta(hours=25), finalized=True)
    assert await mm.scan_stale_conversations() == 0


@pytest.mark.asyncio
async def test_scan_respects_limit(db, monkeypatch):
    import src.conversations.long_term_memory as ltm
    import src.conversations.memory_maintenance as mm

    calls: list[tuple] = []

    async def _fake_extract(cid, uid, llm_cfg=None):
        calls.append((cid, uid))
        return 1

    monkeypatch.setattr(ltm, "extract_conversation_memories", _fake_extract)

    for _ in range(mm._STALE_SCAN_LIMIT + 5):     # 25 stale conversations
        await _seed_conv(db, "u1", updated_at=_now() - timedelta(hours=30))

    # First round caps at the LIMIT; the rest wait for the next round.
    assert await mm.scan_stale_conversations() == mm._STALE_SCAN_LIMIT
    assert len(calls) == mm._STALE_SCAN_LIMIT


@pytest.mark.asyncio
async def test_scan_concurrent_with_manual_finalize_extracts_once(db, monkeypatch):
    """A background scan racing a concurrent MANUAL finalize on the same open
    conversation must extract exactly once — both go through the identical CAS
    gate (UPDATE ... WHERE finalized_at IS NULL), so only the rowcount==1 winner
    extracts. Mirrors routes.finalize_conversation's gate."""
    import src.conversations.long_term_memory as ltm
    import src.conversations.memory_maintenance as mm
    from src.conversations.models import Conversation
    from src.infra.database import get_session_factory
    from sqlalchemy import update as sa_update

    calls: list[tuple] = []

    async def _fake_extract(cid, uid, llm_cfg=None):
        await asyncio.sleep(0)          # force interleave with the racing finalize
        calls.append((cid, uid))
        return 2

    monkeypatch.setattr(ltm, "extract_conversation_memories", _fake_extract)

    conv_id = await _seed_conv(db, "u1", updated_at=_now() - timedelta(hours=25))

    async def _manual_finalize():
        # The same CAS the finalize route runs; on the win, extract once.
        async with get_session_factory()() as s:
            res = await s.execute(
                sa_update(Conversation)
                .where(Conversation.id == conv_id, Conversation.finalized_at.is_(None))
                .values(finalized_at=_now())
            )
            await s.commit()
        if (res.rowcount or 0) == 1:
            await ltm.extract_conversation_memories(conv_id, "u1")

    await asyncio.gather(mm.scan_stale_conversations(), _manual_finalize())

    # Exactly one of the two writers won the CAS → extraction ran once.
    assert calls == [(conv_id, "u1")]


# =========================================================================== #
# L2 touch — recall bumps access_count / last_accessed_at (fire-and-forget)
# =========================================================================== #
@pytest.mark.asyncio
async def test_recall_touches_access_count(db, monkeypatch):
    import src.conversations.long_term_memory as ltm

    async def _none(uid):
        return None

    monkeypatch.setattr(ltm, "_resolve_user_embedding_by_id", _none)

    m = await _seed(db, "u1", content="被访问", importance=0.5)
    res = await ltm.retrieve_long_term_memories("u1", "查询")
    assert [x["id"] for x in res] == [m]

    # Drain the detached touch task, then assert the counters advanced.
    await asyncio.gather(*list(ltm._bg_tasks))
    row = await _get(db, m)
    assert row.access_count == 1
    assert row.last_accessed_at is not None


# =========================================================================== #
# Scheduler — enable gate, start/stop lifecycle, round orchestration
# =========================================================================== #
@pytest.mark.asyncio
async def test_start_disabled_returns_none(monkeypatch):
    import src.conversations.memory_maintenance as mm
    from src.settings import get_settings

    monkeypatch.setenv("MEMORY_MAINTENANCE_ENABLED", "false")
    get_settings.cache_clear()
    assert mm.start_memory_maintenance() is None
    assert mm._maintenance_task is None


@pytest.mark.asyncio
async def test_start_then_stop_cancels_cleanly(monkeypatch):
    import src.conversations.memory_maintenance as mm
    from src.settings import get_settings

    monkeypatch.setenv("MEMORY_MAINTENANCE_ENABLED", "true")
    monkeypatch.setenv("MEMORY_MAINTENANCE_HOUR", "3")
    get_settings.cache_clear()

    task = mm.start_memory_maintenance()
    assert task is not None and not task.done()
    # Idempotent: a second start returns the SAME task, not a new one.
    assert mm.start_memory_maintenance() is task

    await mm.stop_memory_maintenance()
    assert task.done()
    assert mm._maintenance_task is None


@pytest.mark.asyncio
async def test_run_round_scans_then_maintains_each_user(db, monkeypatch):
    import src.conversations.memory_maintenance as mm

    scanned: list[int] = []
    maintained: list[str] = []

    async def _fake_scan():
        scanned.append(1)
        return 0

    async def _fake_maintain(uid):
        maintained.append(uid)
        return {}

    monkeypatch.setattr(mm, "scan_stale_conversations", _fake_scan)
    monkeypatch.setattr(mm, "maintain_user_memories", _fake_maintain)

    await _seed(db, "u1", content="x")
    await _seed(db, "u2", content="y")
    await _seed(db, "u2", content="z")   # u2 twice → still one distinct maintain call

    summary = await mm.run_maintenance_round()
    assert scanned == [1]                       # auto_extract on by default
    assert sorted(maintained) == ["u1", "u2"]   # distinct users
    assert summary["users"] == 2


@pytest.mark.asyncio
async def test_run_round_skips_scan_when_auto_extract_off(db, monkeypatch):
    import src.conversations.memory_maintenance as mm
    from src.settings import get_settings

    monkeypatch.setenv("MEMORY_AUTO_EXTRACT", "false")
    get_settings.cache_clear()

    scanned: list[int] = []
    maintained: list[str] = []

    async def _fake_scan():
        scanned.append(1)
        return 0

    async def _fake_maintain(uid):
        maintained.append(uid)
        return {}

    monkeypatch.setattr(mm, "scan_stale_conversations", _fake_scan)
    monkeypatch.setattr(mm, "maintain_user_memories", _fake_maintain)

    await _seed(db, "u1", content="x")
    await mm.run_maintenance_round()

    # Scan (memory-creating) is gated off; maintenance (pruning) still runs.
    assert scanned == []
    assert maintained == ["u1"]


def test_seconds_until_maintenance_hour():
    from src.conversations.memory_maintenance import _seconds_until_maintenance_hour

    base = datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)
    assert _seconds_until_maintenance_hour(base, 3) == 3600.0          # 02:00 → 03:00
    # Past today's hour → rolls to tomorrow (04:00 → next-day 03:00 = 23h).
    later = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)
    assert _seconds_until_maintenance_hour(later, 3) == 23 * 3600.0
