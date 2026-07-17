"""Tests for v3-M3 long-term memory + user profile.

All external services are faked — no real Redis / Milvus / LLM:
  * fakeredis for the profile hot Hash,
  * a capturing OpenAI-shaped client to assert the SESSION LLM is used,
  * an in-memory ``_FakeVectorStore`` for the vector index,
  * a monkeypatched ``embed`` returning a constant vector.

DB-backed cases use the shared ``db`` fixture (temp SQLite; ``init_db`` creates
``user_memories`` via ``create_all``).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select


# --------------------------------------------------------------------------- #
# Global hygiene between tests (Redis singleton + settings cache).
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


def _fake_redis():
    import fakeredis.aioredis as far

    from src.infra.redis_client import RedisClient

    return RedisClient("redis://fake", client=far.FakeRedis(decode_responses=True))


# --------------------------------------------------------------------------- #
# Session-LLM capture + embedding cfg + fake vector store
# --------------------------------------------------------------------------- #
def _session_llm_cfg(model: str = "sess-model"):
    from src.settings_user import UserLLMConfig

    return UserLLMConfig(
        provider="openai-compat", base_url="https://api.example.com",
        api_key="sk-x", default_model=model, complex_model=model,
    )


def _embed_cfg():
    from src.settings_user import UserEmbeddingConfig

    return UserEmbeddingConfig(
        provider="openai-compat", base_url="https://e.example.com",
        api_key="k", model="bge-m3", dim=4,
    )


class _CapturingOpenAIClient:
    """OpenAI-shaped client that records the model used and returns a canned reply."""

    def __init__(self, sink: dict, reply: str):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self._sink = sink
        self._reply = reply

    async def _create(self, **kwargs):
        self._sink["model"] = kwargs.get("model")
        self._sink["calls"] = self._sink.get("calls", 0) + 1
        msg = SimpleNamespace(content=self._reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _capturing_get_client(sink: dict, reply: str):
    def _factory(cfg=None):
        sink["cfg"] = cfg
        return _CapturingOpenAIClient(sink, reply)

    return _factory


def _cos(a, b):
    import math

    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9)


class _FakeVectorStore:
    """In-memory stand-in for a multi-collection backend (Qdrant/Milvus shape).

    ``fail=True`` makes every op raise, to exercise the optional-infra degrade.
    """

    def __init__(self, *, fail: bool = False):
        self.points: dict[str, tuple[list, dict]] = {}
        self.fail = fail
        self.created: list[tuple[str, int]] = []
        self.deleted_filters: list[dict] = []

    async def create_collection(self, name, vector_size):
        if self.fail:
            raise RuntimeError("boom-create")
        self.created.append((name, vector_size))

    async def upsert(self, points, collection_name=None):
        if self.fail:
            raise RuntimeError("boom-upsert")
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


async def _const_embed(text, cfg=None):
    return [1.0, 0.0, 0.0, 0.0]


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
async def _make_memory(
    database, user_id, mtype, content, importance, *, created_at=None, mem_id=None
) -> str:
    from src.conversations.models import UserMemory

    mid = mem_id or str(uuid.uuid4())
    async with database.get_session_factory()() as s:
        s.add(UserMemory(
            id=mid, user_id=user_id, memory_type=mtype, content=content,
            importance=importance,
            created_at=created_at or datetime.now(timezone.utc),
        ))
        await s.commit()
    return mid


async def _make_conv_with_messages(database, user_id, msgs) -> str:
    from src.conversations.models import Conversation, Message

    conv_id = str(uuid.uuid4())
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with database.get_session_factory()() as s:
        s.add(Conversation(id=conv_id, user_id=user_id, title="t", kb_id=None))
        for i, (role, content) in enumerate(msgs):
            s.add(Message(
                id=f"{conv_id}-{i}", conversation_id=conv_id, role=role,
                content=content, created_at=base + timedelta(seconds=i),
            ))
        await s.commit()
    return conv_id


# --------------------------------------------------------------------------- #
# Robust JSON parsing (never raises)
# --------------------------------------------------------------------------- #
def test_parse_object_plain_fenced_and_prose():
    from src.conversations.long_term_memory import parse_memory_object

    plain = parse_memory_object('{"type":"profile","content":"后端","importance":0.7}')
    assert plain["type"] == "profile" and plain["content"] == "后端"

    fenced = parse_memory_object('```json\n{"type":"fact","content":"K8s","importance":0.5}\n```')
    assert fenced["type"] == "fact"

    prosed = parse_memory_object('好的，结果：{"type":"skill","content":"Docker","importance":0.6} 完毕')
    assert prosed["content"] == "Docker"


def test_parse_object_bad_json_returns_none():
    from src.conversations.long_term_memory import parse_memory_object

    assert parse_memory_object("这不是 JSON") is None
    assert parse_memory_object("{ 坏掉的 json") is None
    assert parse_memory_object("") is None
    # a JSON array is not an object
    assert parse_memory_object("[1,2,3]") is None


def test_parse_array_fenced_prose_and_bad():
    from src.conversations.long_term_memory import parse_memory_array

    fenced = parse_memory_array(
        '```json\n[{"type":"fact","content":"a","importance":0.5},'
        '{"type":"skill","content":"b","importance":0.6}]\n```'
    )
    assert [o["content"] for o in fenced] == ["a", "b"]

    prosed = parse_memory_array('分析结果如下 [{"type":"preference","content":"c","importance":0.4}]。')
    assert prosed[0]["type"] == "preference"

    assert parse_memory_array("完全不是 JSON") == []
    assert parse_memory_array('{"type":"fact"}') == []   # object, not array
    assert parse_memory_array("[坏 json") == []


# --------------------------------------------------------------------------- #
# Keyword extraction: session LLM + write, role gate, no-hit, auto_extract off
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_keyword_extract_uses_session_llm_and_writes(db, monkeypatch):
    from src.conversations import long_term_memory as ltm
    from src.conversations.models import UserMemory

    sink: dict = {}
    monkeypatch.setattr(
        "src.infra.llm.get_client",
        _capturing_get_client(sink, '{"type":"profile","content":"后端工程师，用 Python","importance":0.8}'),
    )

    async def _no_embed(uid):  # isolate: skip the vector path
        return None

    monkeypatch.setattr(ltm, "_resolve_user_embedding_by_id", _no_embed)

    cfg = _session_llm_cfg("sess-model")
    await ltm._keyword_extract("u1", "c1", "我是后端工程师", ["profile"], cfg)

    # The SESSION cfg object itself reached get_client; model is the session
    # default_model — never a hardcoded vendor model (PRD 2026-07-16).
    assert sink["cfg"] is cfg
    assert sink["model"] == "sess-model"

    async with db.get_session_factory()() as s:
        rows = (await s.execute(select(UserMemory).where(UserMemory.user_id == "u1"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].memory_type == "profile"
    assert "后端工程师" in rows[0].content
    assert 0.79 < rows[0].importance < 0.81
    assert rows[0].source_conversation_id == "c1"


@pytest.mark.asyncio
async def test_keyword_extract_drops_empty_llm_result(db, monkeypatch):
    """LLM says 'nothing to remember' → no row written, no exception."""
    from src.conversations import long_term_memory as ltm
    from src.conversations.models import UserMemory

    sink: dict = {}
    monkeypatch.setattr(
        "src.infra.llm.get_client",
        _capturing_get_client(sink, '{"type":"","content":"","importance":0}'),
    )

    async def _no_embed(uid):
        return None

    monkeypatch.setattr(ltm, "_resolve_user_embedding_by_id", _no_embed)
    await ltm._keyword_extract("u1", "c1", "我是工程师", ["profile"], _session_llm_cfg())

    async with db.get_session_factory()() as s:
        rows = (await s.execute(select(UserMemory).where(UserMemory.user_id == "u1"))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_schedule_keyword_extraction_hit_and_gates(db, monkeypatch):
    from src.conversations import long_term_memory as ltm
    from src.conversations.models import UserMemory

    sink: dict = {}
    monkeypatch.setattr(
        "src.infra.llm.get_client",
        _capturing_get_client(sink, '{"type":"preference","content":"Python","importance":0.6}'),
    )

    async def _no_embed(uid):
        return None

    monkeypatch.setattr(ltm, "_resolve_user_embedding_by_id", _no_embed)

    # role != user → no task scheduled.
    ltm.schedule_keyword_extraction("u1", "c1", "assistant", "我喜欢 Python", _session_llm_cfg())
    assert not ltm._bg_tasks

    # user turn with no keyword hit → no task scheduled (only a regex pass).
    ltm.schedule_keyword_extraction("u1", "c1", "user", "今天天气如何", _session_llm_cfg())
    assert not ltm._bg_tasks

    # user turn WITH a hit → one task; drain it deterministically.
    ltm.schedule_keyword_extraction("u1", "c1", "user", "我喜欢 Python", _session_llm_cfg("sess-model"))
    tasks = list(ltm._bg_tasks)
    assert len(tasks) == 1
    await asyncio.gather(*tasks)

    assert sink["model"] == "sess-model"
    async with db.get_session_factory()() as s:
        rows = (await s.execute(select(UserMemory).where(UserMemory.user_id == "u1"))).scalars().all()
    assert len(rows) == 1 and rows[0].memory_type == "preference"


@pytest.mark.asyncio
async def test_auto_extract_off_short_circuits_everything(db, monkeypatch):
    monkeypatch.setenv("MEMORY_AUTO_EXTRACT", "false")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import long_term_memory as ltm
    from src.conversations.models import UserMemory

    def _boom(cfg=None):
        raise AssertionError("LLM must not be called when auto-extract is off")

    monkeypatch.setattr("src.infra.llm.get_client", _boom)

    # Keyword scheduler: no task, no LLM.
    ltm.schedule_keyword_extraction("u1", "c1", "user", "我是工程师", _session_llm_cfg())
    assert not ltm._bg_tasks
    await asyncio.sleep(0.02)

    # Conversation extraction: returns 0 before touching the LLM.
    conv_id = await _make_conv_with_messages(db, "u1", [("user", "我们用 K8s"), ("assistant", "好的")])
    assert await ltm.extract_conversation_memories(conv_id, "u1", _session_llm_cfg()) == 0

    async with db.get_session_factory()() as s:
        rows = (await s.execute(select(UserMemory).where(UserMemory.user_id == "u1"))).scalars().all()
    assert rows == []


# --------------------------------------------------------------------------- #
# Session-end extraction: session LLM, array parse, write
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_extract_conversation_uses_session_llm_and_writes(db, monkeypatch):
    from src.conversations import long_term_memory as ltm
    from src.conversations.models import UserMemory

    sink: dict = {}
    reply = (
        '```json\n[{"type":"fact","content":"团队用 K8s","importance":0.6},'
        '{"type":"skill","content":"Docker","importance":0.7},'
        '{"type":"bogus","content":"x","importance":0.9}]\n```'
    )
    monkeypatch.setattr("src.infra.llm.get_client", _capturing_get_client(sink, reply))

    async def _no_embed(uid):
        return None

    monkeypatch.setattr(ltm, "_resolve_user_embedding_by_id", _no_embed)

    conv_id = await _make_conv_with_messages(db, "u1", [("user", "我们用 K8s"), ("assistant", "了解")])
    cfg = _session_llm_cfg("sess-model")
    n = await ltm.extract_conversation_memories(conv_id, "u1", cfg)

    # Two valid items stored; the unknown-type item is dropped (not stored).
    assert n == 2
    assert sink["cfg"] is cfg and sink["model"] == "sess-model"
    async with db.get_session_factory()() as s:
        rows = (await s.execute(select(UserMemory).where(UserMemory.user_id == "u1"))).scalars().all()
    assert {r.memory_type for r in rows} == {"fact", "skill"}
    assert all(r.source_conversation_id == conv_id for r in rows)


@pytest.mark.asyncio
async def test_extract_conversation_bad_json_writes_nothing(db, monkeypatch):
    """Malformed LLM output → dropped wholesale, 0 stored, never raises."""
    from src.conversations import long_term_memory as ltm
    from src.conversations.models import UserMemory

    monkeypatch.setattr(
        "src.infra.llm.get_client",
        _capturing_get_client({}, "对不起我无法输出 JSON，这是一段自然语言。"),
    )

    async def _no_embed(uid):
        return None

    monkeypatch.setattr(ltm, "_resolve_user_embedding_by_id", _no_embed)
    conv_id = await _make_conv_with_messages(db, "u1", [("user", "随便聊聊")])
    assert await ltm.extract_conversation_memories(conv_id, "u1", _session_llm_cfg()) == 0

    async with db.get_session_factory()() as s:
        rows = (await s.execute(select(UserMemory).where(UserMemory.user_id == "u1"))).scalars().all()
    assert rows == []


# --------------------------------------------------------------------------- #
# User profile (L1): PG aggregate, Redis hot, cross-user isolation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_user_profile_pg_aggregate(db):
    from src.conversations.long_term_memory import get_user_profile

    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    await _make_memory(db, "u1", "profile", "后端工程师", 0.9, created_at=base)
    await _make_memory(db, "u1", "preference", "Python", 0.8, created_at=base)
    await _make_memory(db, "u1", "preference", "PostgreSQL", 0.7, created_at=base)
    await _make_memory(db, "u1", "skill", "Docker", 0.6, created_at=base)
    await _make_memory(db, "u1", "fact", "团队用 K8s 部署", 0.5, created_at=base)

    p = await get_user_profile("u1")
    assert p["role"] == "后端工程师"
    assert p["preferences"] == ["Python", "PostgreSQL"]   # importance DESC
    assert p["skills"] == ["Docker"]
    assert p["environment"] == "团队用 K8s 部署"
    assert p["current_project"] == ""   # no source type in M3


@pytest.mark.asyncio
async def test_get_user_profile_redis_hot_then_pg_unreachable(db, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import long_term_memory as ltm
    from src.infra.redis_client import set_redis

    set_redis(_fake_redis())
    await _make_memory(db, "u1", "profile", "后端工程师", 0.9)

    # First read: PG miss on hot → aggregate → write-back to Redis Hash.
    p1 = await ltm.get_user_profile("u1")
    assert p1["role"] == "后端工程师"

    # Second read must be served from the hot Hash: break PG aggregation and
    # assert it's never consulted.
    async def _boom(uid):
        raise AssertionError("hot cache should have served this read")

    monkeypatch.setattr(ltm, "_aggregate_profile_pg", _boom)
    p2 = await ltm.get_user_profile("u1")
    assert p2["role"] == "后端工程师"


@pytest.mark.asyncio
async def test_get_user_profile_cross_user_isolation(db):
    from src.conversations.long_term_memory import get_user_profile, profile_is_empty

    await _make_memory(db, "userA", "profile", "A 的角色", 0.9)
    await _make_memory(db, "userA", "preference", "A 的偏好", 0.8)

    pa = await get_user_profile("userA")
    pb = await get_user_profile("userB")
    assert pa["role"] == "A 的角色"
    assert profile_is_empty(pb)      # userB has no memories → empty profile
    assert not profile_is_empty(pa)


@pytest.mark.asyncio
async def test_new_memory_invalidates_profile_cache(db, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import long_term_memory as ltm
    from src.infra.redis_client import set_redis

    rc = _fake_redis()
    set_redis(rc)

    await _make_memory(db, "u1", "profile", "旧角色", 0.5)
    await ltm.get_user_profile("u1")   # primes the hot Hash
    assert await rc._raw.exists(ltm._profile_key("u1"))

    # Persisting a new memory must drop the hot cache so the next read rebuilds.
    async def _no_embed(uid):
        return None

    await ltm._persist_memory("u1", "preference", "新偏好", 0.6, None, None)
    assert not await rc._raw.exists(ltm._profile_key("u1"))
    refreshed = await ltm.get_user_profile("u1")
    assert refreshed["preferences"] == ["新偏好"]


def test_profile_is_empty_matrix():
    from src.conversations.long_term_memory import empty_profile, profile_is_empty

    assert profile_is_empty(None)
    assert profile_is_empty(empty_profile())
    assert profile_is_empty({"role": "  ", "preferences": [""], "skills": []})
    assert not profile_is_empty({"role": "x", "preferences": [], "skills": []})
    assert not profile_is_empty({"role": "", "preferences": ["Python"], "skills": []})


# --------------------------------------------------------------------------- #
# Long-term recall (L2): vector path, PG fallback, degradation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_retrieve_vector_path_returns_indexed_memory(db, monkeypatch):
    from src.conversations import long_term_memory as ltm
    import src.infra.memory_vector as mv

    store = _FakeVectorStore()
    monkeypatch.setattr(mv, "get_store", lambda: store)
    monkeypatch.setattr(ltm, "embed", _const_embed)

    async def _cfg(uid):
        return _embed_cfg()

    monkeypatch.setattr(ltm, "_resolve_user_embedding_by_id", _cfg)

    mid = await ltm._persist_memory("u1", "fact", "团队用 K8s", 0.6, "c1", _embed_cfg())
    assert mid is not None
    assert mid in store.points   # vectorized into the fake index

    res = await ltm.retrieve_long_term_memories("u1", "K8s 部署", limit=3)
    assert any(m["id"] == mid for m in res)
    assert res[0]["content"] == "团队用 K8s"
    assert res[0]["memory_type"] == "fact"


@pytest.mark.asyncio
async def test_retrieve_pg_fallback_when_no_embedding(db, monkeypatch):
    from src.conversations import long_term_memory as ltm

    async def _none(uid):   # user has no embedding cfg → skip vector, PG fallback
        return None

    monkeypatch.setattr(ltm, "_resolve_user_embedding_by_id", _none)

    await _make_memory(db, "u1", "fact", "低重要度", 0.1)
    await _make_memory(db, "u1", "fact", "高重要度", 0.9)

    res = await ltm.retrieve_long_term_memories("u1", "任意问题", limit=3)
    assert [m["content"] for m in res] == ["高重要度", "低重要度"]   # importance DESC


@pytest.mark.asyncio
async def test_retrieve_vector_failure_degrades_to_pg(db, monkeypatch):
    from src.conversations import long_term_memory as ltm
    import src.infra.memory_vector as mv

    store = _FakeVectorStore(fail=True)     # search raises
    monkeypatch.setattr(mv, "get_store", lambda: store)
    monkeypatch.setattr(ltm, "embed", _const_embed)

    async def _cfg(uid):
        return _embed_cfg()

    monkeypatch.setattr(ltm, "_resolve_user_embedding_by_id", _cfg)

    await _make_memory(db, "u1", "fact", "兜底记忆", 0.5)
    res = await ltm.retrieve_long_term_memories("u1", "q", limit=3)
    assert [m["content"] for m in res] == ["兜底记忆"]   # PG fallback, no exception


@pytest.mark.asyncio
async def test_retrieve_embedding_failure_degrades_to_pg(db, monkeypatch):
    """The embedding API is configured but its CALL raises → PG fallback, no raise."""
    from src.conversations import long_term_memory as ltm

    async def _boom_embed(text, cfg=None):
        raise RuntimeError("embedding endpoint 500")

    monkeypatch.setattr(ltm, "embed", _boom_embed)

    async def _cfg(uid):
        return _embed_cfg()

    monkeypatch.setattr(ltm, "_resolve_user_embedding_by_id", _cfg)

    await _make_memory(db, "u1", "fact", "兜底记忆", 0.5)
    res = await ltm.retrieve_long_term_memories("u1", "q", limit=3)
    assert [m["content"] for m in res] == ["兜底记忆"]   # PG fallback despite embed blowing up


@pytest.mark.asyncio
async def test_retrieve_anonymous_returns_empty():
    from src.conversations.long_term_memory import retrieve_long_term_memories

    assert await retrieve_long_term_memories("", "q") == []


# --------------------------------------------------------------------------- #
# Coerce matrix: out-of-range / missing importance, unknown / empty fields
# --------------------------------------------------------------------------- #
def test_coerce_memory_importance_and_type_matrix():
    from src.conversations.long_term_memory import _coerce_memory

    allowed = ("profile", "preference", "fact", "skill")

    # importance clamped into [0, 1]; type normalized (case/whitespace).
    assert _coerce_memory({"type": "FACT", "content": "x", "importance": 5.0}, allowed) == ("fact", "x", 1.0)
    assert _coerce_memory({"type": "fact", "content": "x", "importance": -3}, allowed) == ("fact", "x", 0.0)
    # missing / non-numeric importance → 0.5 default, never raises.
    assert _coerce_memory({"type": "skill", "content": "y"}, allowed) == ("skill", "y", 0.5)
    assert _coerce_memory({"type": "skill", "content": "y", "importance": "abc"}, allowed) == ("skill", "y", 0.5)
    # unknown type or empty content → dropped (None).
    assert _coerce_memory({"type": "bogus", "content": "z", "importance": 0.9}, allowed) is None
    assert _coerce_memory({"type": "fact", "content": "   ", "importance": 0.9}, allowed) is None
    assert _coerce_memory({"content": "no type", "importance": 0.5}, allowed) is None


# --------------------------------------------------------------------------- #
# Vector-layer degradation (optional infra: never raises, PG row survives)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_persist_survives_vector_backend_failure(db, monkeypatch):
    from src.conversations import long_term_memory as ltm
    from src.conversations.models import UserMemory
    import src.infra.memory_vector as mv

    store = _FakeVectorStore(fail=True)     # create/upsert raise
    monkeypatch.setattr(mv, "get_store", lambda: store)
    monkeypatch.setattr(ltm, "embed", _const_embed)

    mid = await ltm._persist_memory("u1", "fact", "x", 0.5, "c1", _embed_cfg())
    assert mid is not None       # PG row written despite the vector failure
    assert store.points == {}    # nothing indexed

    async with db.get_session_factory()() as s:
        row = await s.get(UserMemory, mid)
    assert row is not None and row.content == "x"


@pytest.mark.asyncio
async def test_memory_vector_noops_on_unsupported_backend(monkeypatch):
    import src.infra.memory_vector as mv

    class _LocalLike:
        """Single-collection store (like LocalVectorStore): no create_collection."""

        async def search(self, *a, **k):
            raise AssertionError("search must not be attempted on unsupported backend")

    monkeypatch.setattr(mv, "get_store", lambda: _LocalLike())

    assert await mv.upsert_memory_vector("m", [1.0], "内容", "u", "fact", 0.5) is False
    assert await mv.search_memory_vectors([1.0], "u", 3) == []
    await mv.delete_memory_vectors_by_user("u")   # must not raise
    await mv.delete_memory_vector("m")            # must not raise


@pytest.mark.asyncio
async def test_memory_vector_roundtrip_and_delete(monkeypatch):
    import src.infra.memory_vector as mv

    store = _FakeVectorStore()
    monkeypatch.setattr(mv, "get_store", lambda: store)

    assert await mv.upsert_memory_vector("m1", [1.0, 0.0], "内容一", "u1", "fact", 0.6) is True
    assert await mv.upsert_memory_vector("m2", [0.0, 1.0], "内容二", "u2", "fact", 0.6) is True

    # content lands in the payload as `text` — required by the Milvus hybrid
    # schema's BM25 function (regression guard for the production backend).
    assert store.points["m1"][1]["text"] == "内容一"

    # Scalar-filtered by user_id — u2's vector is invisible to u1.
    ids = await mv.search_memory_vectors([1.0, 0.0], "u1", 3)
    assert ids == ["m1"]

    # Single delete by payload memory_id works on the multi-collection shape.
    await mv.delete_memory_vector("m1")
    assert "m1" not in store.points


# --------------------------------------------------------------------------- #
# purge_user chain: PG rows cleared + vector delete_by_filter(user_id) called
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_purge_user_clears_memories_and_vectors(db, monkeypatch):
    from src.auth.models import User
    from src.auth.password import hash_password
    from src.auth.routes import purge_user
    from src.conversations.models import UserMemory
    import src.infra.memory_vector as mv

    store = _FakeVectorStore()
    monkeypatch.setattr(mv, "get_store", lambda: store)

    factory = db.get_session_factory()
    uid = str(uuid.uuid4())
    async with factory() as s:
        s.add(User(id=uid, email="purge@x.com", password_hash=hash_password("x")))
        await s.commit()

    await _make_memory(db, uid, "fact", "记忆内容", 0.5, mem_id="m1")
    await store.upsert(
        [{"id": "m1", "vector": [1.0, 0.0], "payload": {"user_id": uid, "memory_id": "m1"}}],
        collection_name="user_memory_vectors",
    )

    async with factory() as s:
        u = await s.get(User, uid)
        await purge_user(s, u)

    # PG memory rows gone.
    async with factory() as s:
        rows = (await s.execute(select(UserMemory).where(UserMemory.user_id == uid))).scalars().all()
    assert rows == []
    # Vector chain fired with the user_id filter, and the point is gone.
    assert {"user_id": uid} in store.deleted_filters
    assert "m1" not in store.points


@pytest.mark.asyncio
async def test_purge_user_survives_vector_failure(db, monkeypatch):
    """A vector-backend outage during purge must not break the user deletion."""
    from src.auth.models import User
    from src.auth.password import hash_password
    from src.auth.routes import purge_user
    from src.conversations.models import UserMemory
    import src.infra.memory_vector as mv

    store = _FakeVectorStore(fail=True)
    monkeypatch.setattr(mv, "get_store", lambda: store)

    factory = db.get_session_factory()
    uid = str(uuid.uuid4())
    async with factory() as s:
        s.add(User(id=uid, email="purge2@x.com", password_hash=hash_password("x")))
        await s.commit()
    await _make_memory(db, uid, "fact", "x", 0.5)

    async with factory() as s:
        u = await s.get(User, uid)
        await purge_user(s, u)   # must not raise despite the vector failure

    async with factory() as s:
        rows = (await s.execute(select(UserMemory).where(UserMemory.user_id == uid))).scalars().all()
        gone = await s.get(User, uid)
    assert rows == [] and gone is None


# --------------------------------------------------------------------------- #
# plan_node L1/L2 injection + anonymous skip
# --------------------------------------------------------------------------- #
class _TextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text

    def model_dump(self):
        return {"type": "text", "text": self.text}


class _FakeAnthropicClient:
    def __init__(self, sink: dict):
        self._sink = sink
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self._sink.setdefault("kwargs", []).append(kwargs)
        return SimpleNamespace(
            content=[_TextBlock("好的")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


async def _run_plan(state, monkeypatch):
    from src.agent.nodes import plan_node
    from src.infra.llm import CostTracker
    from src.tools.base import ToolRegistry

    sink: dict = {}
    monkeypatch.setattr("src.agent.nodes.get_client", lambda cfg=None: _FakeAnthropicClient(sink))
    out = await plan_node(
        state, registry=ToolRegistry(), cost=CostTracker(),
        system_prompt="SYS", include_travel_skill=False,
    )
    return out, sink


@pytest.mark.asyncio
async def test_plan_node_injects_l1_l2_and_caches(monkeypatch):
    from src.conversations import long_term_memory as ltm

    async def _prof(uid):
        return {"role": "后端工程师", "preferences": ["Python"], "environment": "",
                "skills": [], "current_project": ""}

    async def _mem(uid, query, limit=3):
        assert query == "继续部署"   # L2 query is the current user input
        return [{"memory_type": "fact", "content": "团队用 K8s"}]

    monkeypatch.setattr(ltm, "get_user_profile", _prof)
    monkeypatch.setattr(ltm, "retrieve_long_term_memories", _mem)

    state = {
        "messages": [{"role": "user", "content": "继续部署"}],
        "iterations": 0, "user_id": "u1",
    }
    out, sink = await _run_plan(state, monkeypatch)

    joined = " ".join(b["text"] for b in sink["kwargs"][0]["system"])
    assert "用户画像" in joined and "后端工程师" in joined
    assert "长期记忆" in joined and "团队用 K8s" in joined
    # Layer order: profile (L1) before long-term memory (L2).
    assert joined.index("用户画像") < joined.index("长期记忆")
    # Cached into state for later tool-loop iterations.
    assert out["user_profile"]["role"] == "后端工程师"
    assert out["long_term_memory"] == [{"memory_type": "fact", "content": "团队用 K8s"}]


@pytest.mark.asyncio
async def test_plan_node_fetches_l1_l2_once_per_request(monkeypatch):
    from src.conversations import long_term_memory as ltm

    calls = {"prof": 0, "mem": 0}

    async def _prof(uid):
        calls["prof"] += 1
        return {"role": "R", "preferences": [], "environment": "", "skills": [], "current_project": ""}

    async def _mem(uid, query, limit=3):
        calls["mem"] += 1
        return [{"memory_type": "fact", "content": "M"}]

    monkeypatch.setattr(ltm, "get_user_profile", _prof)
    monkeypatch.setattr(ltm, "retrieve_long_term_memories", _mem)

    state = {"messages": [{"role": "user", "content": "hi"}], "iterations": 0, "user_id": "u1"}
    out, _ = await _run_plan(state, monkeypatch)
    assert calls == {"prof": 1, "mem": 1}

    # Next plan iteration of the SAME request (state carried over) → no re-fetch.
    state2 = {**out, "final_report": None, "pending_tool_calls": []}
    out2, sink2 = await _run_plan(state2, monkeypatch)
    assert calls == {"prof": 1, "mem": 1}
    joined = " ".join(b["text"] for b in sink2["kwargs"][0]["system"])
    assert "长期记忆" in joined   # still injected from the cache


@pytest.mark.asyncio
async def test_plan_node_anonymous_skips_l1_l2(monkeypatch):
    from src.conversations import long_term_memory as ltm

    async def _boom(*a, **k):
        raise AssertionError("L1/L2 must not be fetched for an anonymous request")

    monkeypatch.setattr(ltm, "get_user_profile", _boom)
    monkeypatch.setattr(ltm, "retrieve_long_term_memories", _boom)

    state = {"messages": [{"role": "user", "content": "hi"}], "iterations": 0}  # no user_id
    out, sink = await _run_plan(state, monkeypatch)

    joined = " ".join(b["text"] for b in sink["kwargs"][0]["system"])
    assert "用户画像" not in joined and "长期记忆" not in joined
    assert out["final_report"] == "好的"


@pytest.mark.asyncio
async def test_plan_node_survives_l1_l2_exceptions(monkeypatch):
    """L1/L2 recall raising (Redis/PG/embedding/vector all down) must not fail
    the chat turn — the layers are dropped and plan_node still produces output."""
    from src.conversations import long_term_memory as ltm

    async def _boom_prof(uid):
        raise RuntimeError("redis + pg both down")

    async def _boom_mem(uid, query, limit=3):
        raise RuntimeError("embedding + vector both down")

    monkeypatch.setattr(ltm, "get_user_profile", _boom_prof)
    monkeypatch.setattr(ltm, "retrieve_long_term_memories", _boom_mem)

    state = {"messages": [{"role": "user", "content": "hi"}], "iterations": 0, "user_id": "u1"}
    out, sink = await _run_plan(state, monkeypatch)

    # Chat succeeds; the failed layers are simply omitted (degraded to empty).
    assert out["final_report"] == "好的"
    joined = " ".join(b["text"] for b in sink["kwargs"][0]["system"])
    assert "用户画像" not in joined and "长期记忆" not in joined
    # Cached as empty so later iterations don't retry the broken reads this request.
    assert out["user_profile"] == {} and out["long_term_memory"] == []
