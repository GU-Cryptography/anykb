"""HTTP tests for v3-M4 memory management routes + conversation finalize.

Covers ``/api/memories`` CRUD (GET/DELETE/PATCH) and
``POST /api/conversations/{id}/finalize`` (PRD §8). All external services are
mocked — no real Redis / Milvus / LLM:
  * fakeredis (injected via ``set_redis``) for the finalize hot-key cleanup,
  * recorders for the vector index (``delete_memory_vector`` /
    ``reindex_memory_vector``) and the profile cache bust,
  * a stub ``extract_conversation_memories`` so finalize asserts wiring, not the
    (separately tested) extraction internals.

Uses the shared ``client`` / ``db`` / ``create_user`` harness (temp SQLite,
``init_db`` builds ``user_memories`` + the finalized_at column). Authorization
is real (``AUTH_ENABLED=true``); tokens are minted with ``issue_token``.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest


# --------------------------------------------------------------------------- #
# Hygiene: reset the Redis singleton + settings cache around every test so an
# injected fakeredis / a REDIS_URL override can't leak between tests.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_memory_globals():
    from src.infra.redis_client import reset_redis
    from src.settings import get_settings

    reset_redis()
    get_settings.cache_clear()
    yield
    reset_redis()
    get_settings.cache_clear()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(user) -> str:
    from src.auth.tokens import issue_token

    return issue_token(user.id, user.email)


def _fake_redis():
    import fakeredis.aioredis as far

    from src.infra.redis_client import RedisClient

    return RedisClient("redis://fake", client=far.FakeRedis(decode_responses=True))


# --------------------------------------------------------------------------- #
# DB seed helpers (direct writes, mirrors test_long_term_memory)
# --------------------------------------------------------------------------- #
async def _seed_memory(
    db, user_id, *, mtype="preference", content="内容", importance=0.5, created_at=None
) -> str:
    from src.conversations.models import UserMemory

    mid = str(uuid.uuid4())
    async with db.get_session_factory()() as s:
        s.add(
            UserMemory(
                id=mid,
                user_id=user_id,
                memory_type=mtype,
                content=content,
                importance=importance,
                source_conversation_id="conv-x",
                created_at=created_at or datetime.now(timezone.utc),
            )
        )
        await s.commit()
    return mid


async def _seed_conversation(db, user_id, *, finalized=False, with_messages=False) -> str:
    from src.conversations.models import Conversation, Message

    cid = str(uuid.uuid4())
    async with db.get_session_factory()() as s:
        s.add(
            Conversation(
                id=cid,
                user_id=user_id,
                title="t",
                kb_id=None,
                finalized_at=datetime.now(timezone.utc) if finalized else None,
            )
        )
        if with_messages:
            base = datetime(2026, 1, 1, tzinfo=timezone.utc)
            s.add(Message(id=f"{cid}-0", conversation_id=cid, role="user",
                          content="我们用 K8s", created_at=base))
            s.add(Message(id=f"{cid}-1", conversation_id=cid, role="assistant",
                          content="好的", created_at=base + timedelta(seconds=1)))
        await s.commit()
    return cid


async def _get_memory_row(db, mem_id):
    from src.conversations.models import UserMemory

    async with db.get_session_factory()() as s:
        return await s.get(UserMemory, mem_id)


# =========================================================================== #
# Auth: every route rejects an anonymous request (401 before any work)
# =========================================================================== #
async def test_memory_routes_require_auth(client):
    assert (await client.get("/api/memories")).status_code == 401
    assert (await client.delete(f"/api/memories/{uuid.uuid4()}")).status_code == 401
    assert (
        await client.patch(f"/api/memories/{uuid.uuid4()}", json={"importance": 0.5})
    ).status_code == 401
    assert (
        await client.post(f"/api/conversations/{uuid.uuid4()}/finalize")
    ).status_code == 401


# =========================================================================== #
# GET /api/memories — pagination, type filter, ordering, isolation
# =========================================================================== #
async def test_list_pagination_and_ordering(client, create_user, db):
    user = await create_user("list@x.com")
    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    # Seed 5 rows with increasing created_at → newest is #4.
    for i in range(5):
        await _seed_memory(
            db, user.id, content=f"m{i}", created_at=base + timedelta(minutes=i)
        )

    r = await client.get("/api/memories?limit=2&offset=0", headers=_headers(_token(user)))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert body["limit"] == 2 and body["offset"] == 0
    # created_at DESC → newest two first.
    assert [m["content"] for m in body["memories"]] == ["m4", "m3"]
    # DTO shape (PRD §8 GET response).
    first = body["memories"][0]
    assert set(first) == {
        "id", "type", "content", "importance", "source_conversation_id", "created_at"
    }

    r2 = await client.get("/api/memories?limit=2&offset=2", headers=_headers(_token(user)))
    assert [m["content"] for m in r2.json()["memories"]] == ["m2", "m1"]


async def test_list_type_filter_and_invalid_type(client, create_user, db):
    user = await create_user("filter@x.com")
    for _ in range(3):
        await _seed_memory(db, user.id, mtype="preference", content="p")
    for _ in range(2):
        await _seed_memory(db, user.id, mtype="fact", content="f")

    r = await client.get("/api/memories?type=preference", headers=_headers(_token(user)))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert all(m["type"] == "preference" for m in body["memories"])

    # Unknown type value → 422 (Literal query validation).
    bad = await client.get("/api/memories?type=bogus", headers=_headers(_token(user)))
    assert bad.status_code == 422

    # limit above the 200 cap → 422.
    over = await client.get("/api/memories?limit=500", headers=_headers(_token(user)))
    assert over.status_code == 422


async def test_list_is_owner_scoped(client, create_user, db):
    a = await create_user("a-list@x.com")
    b = await create_user("b-list@x.com")
    await _seed_memory(db, b.id, content="B 的记忆")

    r = await client.get("/api/memories", headers=_headers(_token(a)))
    assert r.status_code == 200
    assert r.json() == {
        "total": 0,
        "limit": 50,
        "offset": 0,
        "memories": [],
        "stats": {"by_type": {}, "active_total": 0},
    }


# =========================================================================== #
# GET /api/memories — v3-M5 stats + soft-delete exclusion
# =========================================================================== #
async def test_list_stats_by_type_and_active_total(client, create_user, db):
    """stats is a filter-independent summary of the user's ACTIVE memories."""
    user = await create_user("stats@x.com")
    for _ in range(3):
        await _seed_memory(db, user.id, mtype="preference", content="p")
    for _ in range(2):
        await _seed_memory(db, user.id, mtype="fact", content="f")
    await _seed_memory(db, user.id, mtype="skill", content="s")

    # Even with a ?type= filter, stats reflects ALL active memories, not the page.
    r = await client.get("/api/memories?type=fact", headers=_headers(_token(user)))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2  # the filtered list total
    assert body["stats"]["by_type"] == {"preference": 3, "fact": 2, "skill": 1}
    assert body["stats"]["active_total"] == 6


async def test_list_and_stats_exclude_soft_deleted(client, create_user, db):
    """v3-M5: soft-deleted rows vanish from the list, total, AND stats."""
    from datetime import datetime, timezone

    from src.conversations.models import UserMemory

    user = await create_user("softdel@x.com")
    live = await _seed_memory(db, user.id, mtype="fact", content="活着")
    dead = await _seed_memory(db, user.id, mtype="fact", content="已软删")

    # Soft-delete one row directly (as the maintenance job would).
    async with db.get_session_factory()() as s:
        row = await s.get(UserMemory, dead)
        row.deleted_at = datetime.now(timezone.utc)
        await s.commit()

    r = await client.get("/api/memories", headers=_headers(_token(user)))
    body = r.json()
    assert body["total"] == 1
    assert [m["id"] for m in body["memories"]] == [live]
    assert body["stats"] == {"by_type": {"fact": 1}, "active_total": 1}


async def test_patch_and_delete_soft_deleted_row_404(client, create_user, db):
    """v3-M5: a soft-deleted row is gone to the user — PATCH/DELETE both 404,
    so the maintenance janitor's cull can't be resurrected or double-deleted."""
    from datetime import datetime, timezone

    from src.conversations.models import UserMemory

    user = await create_user("softdel404@x.com")
    dead = await _seed_memory(db, user.id, mtype="fact", content="已软删")
    async with db.get_session_factory()() as s:
        row = await s.get(UserMemory, dead)
        row.deleted_at = datetime.now(timezone.utc)
        await s.commit()

    r = await client.patch(
        f"/api/memories/{dead}", json={"content": "复活?"}, headers=_headers(_token(user))
    )
    assert r.status_code == 404
    r = await client.delete(f"/api/memories/{dead}", headers=_headers(_token(user)))
    assert r.status_code == 404

    # The row is untouched (content not resurrected, deleted_at intact).
    async with db.get_session_factory()() as s:
        row = await s.get(UserMemory, dead)
        assert row is not None and row.content == "已软删" and row.deleted_at is not None


# =========================================================================== #
# DELETE /api/memories/{id} — owner scope, PG + vector chain, 404 isolation
# =========================================================================== #
async def test_delete_removes_row_and_calls_vector(client, create_user, db, monkeypatch):
    user = await create_user("del@x.com")
    mem_id = await _seed_memory(db, user.id, content="待删")

    deleted: list[str] = []

    async def _rec_delete(mid):
        deleted.append(mid)

    invalidated: list[str] = []

    async def _rec_invalidate(uid):
        invalidated.append(uid)

    monkeypatch.setattr("src.infra.memory_vector.delete_memory_vector", _rec_delete)
    monkeypatch.setattr(
        "src.conversations.long_term_memory.invalidate_profile_cache", _rec_invalidate
    )

    r = await client.delete(f"/api/memories/{mem_id}", headers=_headers(_token(user)))
    assert r.status_code == 204

    # PG row gone + vector delete + cache bust all fired.
    assert await _get_memory_row(db, mem_id) is None
    assert deleted == [mem_id]
    assert invalidated == [user.id]


async def test_delete_foreign_memory_404(client, create_user, db, monkeypatch):
    a = await create_user("a-del@x.com")
    b = await create_user("b-del@x.com")
    mem_id = await _seed_memory(db, b.id, content="B 的记忆")

    called: list[str] = []
    monkeypatch.setattr(
        "src.infra.memory_vector.delete_memory_vector",
        lambda mid: called.append(mid),  # noqa: ARG005 — must NOT be reached
    )

    r = await client.delete(f"/api/memories/{mem_id}", headers=_headers(_token(a)))
    assert r.status_code == 404
    # B's row untouched, and the vector chain never ran for a 404.
    assert await _get_memory_row(db, mem_id) is not None
    assert called == []


async def test_delete_missing_memory_404(client, create_user):
    user = await create_user("del404@x.com")
    r = await client.delete(f"/api/memories/{uuid.uuid4()}", headers=_headers(_token(user)))
    assert r.status_code == 404


# =========================================================================== #
# PATCH /api/memories/{id} — content/importance edit, clamp, reindex, 422s
# =========================================================================== #
async def test_patch_content_reindexes_and_invalidates(client, create_user, db, monkeypatch):
    user = await create_user("patch1@x.com")
    mem_id = await _seed_memory(db, user.id, mtype="fact", content="旧内容", importance=0.4)

    reindexed: list[tuple] = []

    async def _rec_reindex(mid, uid, mtype, content, importance):
        reindexed.append((mid, uid, mtype, content, importance))

    invalidated: list[str] = []

    async def _rec_invalidate(uid):
        invalidated.append(uid)

    monkeypatch.setattr(
        "src.conversations.long_term_memory.reindex_memory_vector", _rec_reindex
    )
    monkeypatch.setattr(
        "src.conversations.long_term_memory.invalidate_profile_cache", _rec_invalidate
    )

    r = await client.patch(
        f"/api/memories/{mem_id}",
        json={"content": "  新内容  "},
        headers=_headers(_token(user)),
    )
    assert r.status_code == 200
    dto = r.json()
    assert dto["content"] == "新内容"          # stripped
    assert dto["type"] == "fact"

    row = await _get_memory_row(db, mem_id)
    assert row.content == "新内容"
    # Reindex called with the post-edit content + unchanged type/importance.
    assert reindexed == [(mem_id, user.id, "fact", "新内容", 0.4)]
    assert invalidated == [user.id]


async def test_patch_importance_clamps_and_reindexes(client, create_user, db, monkeypatch):
    user = await create_user("patch2@x.com")
    mem_id = await _seed_memory(db, user.id, content="c", importance=0.5)

    reindexed: list[tuple] = []

    async def _rec_reindex(mid, uid, mtype, content, importance):
        reindexed.append((mid, uid, mtype, content, importance))

    monkeypatch.setattr(
        "src.conversations.long_term_memory.reindex_memory_vector", _rec_reindex
    )

    async def _noop_invalidate(uid):
        return None

    monkeypatch.setattr(
        "src.conversations.long_term_memory.invalidate_profile_cache", _noop_invalidate
    )

    # Over-range importance is clamped to 1.0 (not rejected).
    r = await client.patch(
        f"/api/memories/{mem_id}", json={"importance": 5.0}, headers=_headers(_token(user))
    )
    assert r.status_code == 200
    assert r.json()["importance"] == 1.0
    assert (await _get_memory_row(db, mem_id)).importance == 1.0
    # Importance-only edit still re-indexes (re-embeds the unchanged content).
    assert reindexed[-1] == (mem_id, user.id, "preference", "c", 1.0)

    # Negative clamps to 0.0.
    r2 = await client.patch(
        f"/api/memories/{mem_id}", json={"importance": -3}, headers=_headers(_token(user))
    )
    assert r2.json()["importance"] == 0.0


async def test_patch_empty_body_and_blank_content_422(client, create_user, db, monkeypatch):
    user = await create_user("patch3@x.com")
    mem_id = await _seed_memory(db, user.id, content="orig")

    reindexed: list = []
    monkeypatch.setattr(
        "src.conversations.long_term_memory.reindex_memory_vector",
        lambda *a: reindexed.append(a),  # must NOT be reached
    )
    monkeypatch.setattr(
        "src.conversations.long_term_memory.invalidate_profile_cache",
        lambda uid: None,  # noqa: ARG005
    )

    # Empty body → nothing to update → 422.
    r = await client.patch(f"/api/memories/{mem_id}", json={}, headers=_headers(_token(user)))
    assert r.status_code == 422

    # Whitespace-only content → 422 (Field min_length=1 passes, handler strips).
    r2 = await client.patch(
        f"/api/memories/{mem_id}", json={"content": "   "}, headers=_headers(_token(user))
    )
    assert r2.status_code == 422

    # Empty-string content → 422 at validation (min_length=1).
    r3 = await client.patch(
        f"/api/memories/{mem_id}", json={"content": ""}, headers=_headers(_token(user))
    )
    assert r3.status_code == 422

    # None of the rejected requests touched the row or the vector index.
    assert (await _get_memory_row(db, mem_id)).content == "orig"
    assert reindexed == []


async def test_patch_foreign_memory_404(client, create_user, db, monkeypatch):
    a = await create_user("a-patch@x.com")
    b = await create_user("b-patch@x.com")
    mem_id = await _seed_memory(db, b.id, content="B 的记忆")

    monkeypatch.setattr(
        "src.conversations.long_term_memory.reindex_memory_vector",
        lambda *a: pytest.fail("must not reindex a foreign memory"),
    )

    r = await client.patch(
        f"/api/memories/{mem_id}", json={"content": "hack"}, headers=_headers(_token(a))
    )
    assert r.status_code == 404
    assert (await _get_memory_row(db, mem_id)).content == "B 的记忆"


# =========================================================================== #
# POST /api/conversations/{id}/finalize — extract, idempotency, owner, cleanup
# =========================================================================== #
async def test_finalize_extracts_and_is_idempotent(client, create_user, db, monkeypatch):
    user = await create_user("fin1@x.com")
    conv_id = await _seed_conversation(db, user.id, with_messages=True)

    calls: list[tuple] = []

    async def _fake_extract(cid, uid, llm_cfg=None):
        calls.append((cid, uid))
        return 3

    monkeypatch.setattr(
        "src.conversations.long_term_memory.extract_conversation_memories", _fake_extract
    )

    r = await client.post(
        f"/api/conversations/{conv_id}/finalize", headers=_headers(_token(user))
    )
    assert r.status_code == 200
    assert r.json() == {
        "memory_extracted": 3,
        "profile_updated": True,
        "already_finalized": False,
    }
    assert calls == [(conv_id, user.id)]

    # finalized_at is now stamped in PG.
    from src.conversations.models import Conversation

    async with db.get_session_factory()() as s:
        conv = await s.get(Conversation, conv_id)
    assert conv.finalized_at is not None

    # Second call → no-op: already_finalized, and extraction NOT re-run.
    r2 = await client.post(
        f"/api/conversations/{conv_id}/finalize", headers=_headers(_token(user))
    )
    assert r2.status_code == 200
    assert r2.json() == {
        "memory_extracted": 0,
        "profile_updated": False,
        "already_finalized": True,
    }
    assert calls == [(conv_id, user.id)]   # unchanged — extractor untouched


async def test_finalize_zero_memories_profile_not_updated(client, create_user, db, monkeypatch):
    user = await create_user("fin2@x.com")
    conv_id = await _seed_conversation(db, user.id, with_messages=True)

    async def _extract_zero(cid, uid, llm_cfg=None):
        return 0

    monkeypatch.setattr(
        "src.conversations.long_term_memory.extract_conversation_memories", _extract_zero
    )

    r = await client.post(
        f"/api/conversations/{conv_id}/finalize", headers=_headers(_token(user))
    )
    assert r.json() == {
        "memory_extracted": 0,
        "profile_updated": False,
        "already_finalized": False,
    }


async def test_finalize_foreign_conversation_404(client, create_user, db, monkeypatch):
    a = await create_user("a-fin@x.com")
    b = await create_user("b-fin@x.com")
    conv_id = await _seed_conversation(db, b.id, with_messages=True)

    monkeypatch.setattr(
        "src.conversations.long_term_memory.extract_conversation_memories",
        lambda *a, **k: pytest.fail("must not extract from a foreign conversation"),
    )

    r = await client.post(
        f"/api/conversations/{conv_id}/finalize", headers=_headers(_token(a))
    )
    assert r.status_code == 404
    # B's conversation stays open (never finalized by A's failed attempt).
    from src.conversations.models import Conversation

    async with db.get_session_factory()() as s:
        conv = await s.get(Conversation, conv_id)
    assert conv.finalized_at is None


async def test_finalize_auto_extract_off_returns_zero(client, create_user, db, monkeypatch):
    """auto-extract off → the real extractor short-circuits to 0 (no LLM), and
    finalize reports 0 without rolling back finalized_at (expected behavior)."""
    monkeypatch.setenv("MEMORY_AUTO_EXTRACT", "false")
    from src.settings import get_settings

    get_settings.cache_clear()

    # A real get_client call would prove the LLM was (wrongly) invoked.
    def _boom(cfg=None):
        raise AssertionError("LLM must not be called when auto-extract is off")

    monkeypatch.setattr("src.infra.llm.get_client", _boom)

    user = await create_user("fin3@x.com")
    conv_id = await _seed_conversation(db, user.id, with_messages=True)

    r = await client.post(
        f"/api/conversations/{conv_id}/finalize", headers=_headers(_token(user))
    )
    assert r.status_code == 200
    assert r.json() == {
        "memory_extracted": 0,
        "profile_updated": False,
        "already_finalized": False,
    }
    # Still stamped — "session ended, extraction was attempted".
    from src.conversations.models import Conversation

    async with db.get_session_factory()() as s:
        conv = await s.get(Conversation, conv_id)
    assert conv.finalized_at is not None


async def test_finalize_clears_redis_hot_keys(client, create_user, db, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.infra.redis_client import set_redis

    rc = _fake_redis()
    set_redis(rc)

    async def _extract_one(cid, uid, llm_cfg=None):
        return 1

    monkeypatch.setattr(
        "src.conversations.long_term_memory.extract_conversation_memories", _extract_one
    )

    user = await create_user("fin4@x.com")
    conv_id = await _seed_conversation(db, user.id, with_messages=True)

    # Pre-seed the conversation's hot keys (window + meta), as the M2 path would.
    from src.conversations.short_term_memory import _messages_key, _meta_key

    mkey = _messages_key(user.id, conv_id)
    metakey = _meta_key(user.id, conv_id)
    await rc._raw.lpush(mkey, "msg")
    await rc._raw.hset(metakey, mapping={"context_summary": "s"})
    assert await rc._raw.exists(mkey)
    assert await rc._raw.exists(metakey)

    r = await client.post(
        f"/api/conversations/{conv_id}/finalize", headers=_headers(_token(user))
    )
    assert r.status_code == 200
    assert r.json()["memory_extracted"] == 1

    # Both hot keys cleared by finalize; the durable PG copy is unaffected.
    assert not await rc._raw.exists(mkey)
    assert not await rc._raw.exists(metakey)


async def test_finalize_concurrent_double_extracts_once(client, create_user, db, monkeypatch):
    """Concurrency: two finalize requests racing on the SAME open conversation
    must extract exactly ONCE. The finalized_at stamp is an atomic CAS gate
    (UPDATE ... WHERE finalized_at IS NULL), not a read-modify-write — so one
    request wins (already_finalized=False, extraction runs) and the other is a
    no-op (already_finalized=True, no second extraction → no duplicate memories).
    A read-modify-write would let both pass the NULL check and double-extract.
    """
    user = await create_user("fin-cas@x.com")
    conv_id = await _seed_conversation(db, user.id, with_messages=True)

    calls: list[tuple] = []

    async def _fake_extract(cid, uid, llm_cfg=None):
        # A yield point so the two requests genuinely interleave (the winner is
        # mid-extraction when the loser hits the CAS), stressing the gate.
        await asyncio.sleep(0)
        calls.append((cid, uid))
        return 2

    monkeypatch.setattr(
        "src.conversations.long_term_memory.extract_conversation_memories", _fake_extract
    )

    r1, r2 = await asyncio.gather(
        client.post(f"/api/conversations/{conv_id}/finalize", headers=_headers(_token(user))),
        client.post(f"/api/conversations/{conv_id}/finalize", headers=_headers(_token(user))),
    )
    assert r1.status_code == 200 and r2.status_code == 200

    # Exactly one winner + one no-op, regardless of which coroutine got there first.
    flags = sorted([r1.json()["already_finalized"], r2.json()["already_finalized"]])
    assert flags == [False, True]
    winner = r1.json() if not r1.json()["already_finalized"] else r2.json()
    assert winner["memory_extracted"] == 2
    # The decisive assertion: extraction ran exactly once (no duplicate memories).
    assert calls == [(conv_id, user.id)]

    # finalized_at stamped (the single winner's timestamp).
    from src.conversations.models import Conversation

    async with db.get_session_factory()() as s:
        conv = await s.get(Conversation, conv_id)
    assert conv.finalized_at is not None


# =========================================================================== #
# Migration: conversations.finalized_at added to a legacy table, idempotently
# (mirrors test_admin's cross-dialect ALTER guard — TIMESTAMP WITH TIME ZONE is
# portable across SQLite dev + PostgreSQL prod).
# =========================================================================== #
def test_migration_adds_finalized_at_and_is_idempotent(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from src.infra.database import _migrate_additive_columns

    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    # A legacy conversations table predating the memory-optimization columns.
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE conversations "
                "(id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36), title VARCHAR(128))"
            )
        )
        conn.execute(
            text("INSERT INTO conversations (id, user_id, title) VALUES ('c1', 'u1', 't')")
        )

    # Idempotent — two "startups" must both succeed.
    for _ in range(2):
        with engine.begin() as conn:
            _migrate_additive_columns(conn)

    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("conversations")}
    assert "finalized_at" in cols

    # Existing row backfilled with NULL (still-open conversation).
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT finalized_at FROM conversations WHERE id = 'c1'")
        ).one()
    assert row[0] is None

