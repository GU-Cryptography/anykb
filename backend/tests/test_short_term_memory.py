"""Tests for v3-M2 short-term memory: Redis window, batch compression, L4.

Split into:
  * Pure-unit L4 tests (no DB / Redis / LLM) — build_context_sections early_summary.
  * Redis window tests — fakeredis injected via set_redis (no real server).
  * Compression tests — DB-backed (conftest `db` fixture) with a fake session
    LLM to assert the SESSION model is used, never a hardcoded vendor/model.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.agent.prompts import build_context_sections, SYSTEM_PROMPT_GENERAL
from src.agent.context_builder import build_layered_prompt


# --------------------------------------------------------------------------- #
# Fixtures: keep the Redis singleton + settings cache clean between tests.
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
    """A RedisClient wrapping an in-memory fakeredis async client."""
    import fakeredis.aioredis as far

    from src.infra.redis_client import RedisClient

    return RedisClient("redis://fake", client=far.FakeRedis(decode_responses=True))


# --------------------------------------------------------------------------- #
# L4 early-summary section (pure unit)
# --------------------------------------------------------------------------- #
def test_l4_section_emitted_when_summary_present():
    sections = build_context_sections(
        "SYS", recent_messages=[{"role": "user", "content": "hi"}],
        early_summary="用户想部署 FastAPI 到 K8s",
    )
    l4 = next((s for s in sections if s.layer == 4), None)
    assert l4 is not None
    assert l4.role == "system"
    assert l4.truncatable is False           # L0-L4 never truncated (M1 contract)
    assert l4.section_key == "early_summary"
    assert l4.budget == 300
    assert "用户想部署 FastAPI 到 K8s" in l4.content


def test_l4_section_absent_when_summary_empty():
    """Empty summary → no L4 layer → identical shape to M1 callers."""
    for summary in ("", "   ", None):
        sections = build_context_sections(
            "SYS", recent_messages=[{"role": "user", "content": "hi"}],
            early_summary=summary or "",
        )
        assert all(s.layer != 4 for s in sections)


def test_l4_injected_into_system_text_not_messages():
    msgs = [{"role": "user", "content": "问题"}]
    sections = build_context_sections(
        SYSTEM_PROMPT_GENERAL, recent_messages=msgs, early_summary="早期背景摘要",
    )
    layered = build_layered_prompt(sections, total_budget=8000)
    # L4 is a system-role section → lands in system text, not the messages array.
    assert "早期背景摘要" in layered.system_text
    assert layered.messages == msgs
    assert layered.over_budget is False


def test_no_summary_arg_is_m1_equivalent():
    """Backward-compat: omitting early_summary reproduces the exact M1 output."""
    msgs = [{"role": "user", "content": "你好"}]
    m1 = build_context_sections(SYSTEM_PROMPT_GENERAL, recent_messages=msgs)
    m2 = build_context_sections(
        SYSTEM_PROMPT_GENERAL, recent_messages=msgs, early_summary="",
    )
    assert [(s.layer, s.section_key) for s in m1] == [(s.layer, s.section_key) for s in m2]
    assert build_layered_prompt(m1).system_text == build_layered_prompt(m2).system_text


# --------------------------------------------------------------------------- #
# Redis window: LPUSH + LTRIM + TTL
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_record_message_bounds_window_and_sets_ttl(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("MEMORY_WINDOW_SIZE", "2")   # window*2 = 4 newest kept
    from src.settings import get_settings

    get_settings.cache_clear()

    from src.infra.redis_client import set_redis
    from src.conversations import short_term_memory as stm

    rc = _fake_redis()
    set_redis(rc)
    raw = rc._raw  # fakeredis handle for direct assertions

    uid, cid = "u1", "c1"
    for i in range(6):
        await stm.record_message(uid, cid, "user", f"m{i}")

    key = stm._messages_key(uid, cid)
    # window_size=2 → LTRIM keeps 2*2 = 4 newest messages.
    assert await raw.llen(key) == 4
    # TTL refreshed on write.
    assert await raw.ttl(key) > 0
    # Read back returns chronological order (oldest→newest of the survivors).
    got = await stm.read_window_messages(uid, cid)
    assert [m["content"] for m in got] == ["m2", "m3", "m4", "m5"]


@pytest.mark.asyncio
async def test_record_message_noop_when_feature_off(monkeypatch):
    """Empty REDIS_URL → record_message no-ops (pure-M1 path, no error)."""
    monkeypatch.setenv("REDIS_URL", "")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import short_term_memory as stm

    assert stm.short_term_memory_enabled() is False
    # Must not raise even though no Redis is available.
    await stm.record_message("u", "c", "user", "hi")
    assert await stm.read_window_messages("u", "c") == []


# --------------------------------------------------------------------------- #
# Compression: DB-backed, session-LLM, trigger arithmetic
# --------------------------------------------------------------------------- #
async def _make_conv_with_rounds(database, n_rounds: int, *, user_id: str = "owner"):
    """Insert a conversation + n_rounds (user+assistant) messages, return conv id."""
    from src.conversations.models import Conversation, Message

    factory = database.get_session_factory()
    conv_id = str(uuid.uuid4())
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with factory() as session:
        session.add(Conversation(id=conv_id, user_id=user_id, title="t", kb_id=None))
        for i in range(n_rounds):
            session.add(Message(
                id=f"{conv_id}-u{i}", conversation_id=conv_id, role="user",
                content=f"问题{i}", created_at=base + timedelta(seconds=2 * i),
            ))
            session.add(Message(
                id=f"{conv_id}-a{i}", conversation_id=conv_id, role="assistant",
                content=f"回答{i}", created_at=base + timedelta(seconds=2 * i + 1),
            ))
        await session.commit()
    return conv_id


def _session_llm_cfg(model: str = "sess-model"):
    from src.settings_user import UserLLMConfig

    return UserLLMConfig(
        provider="openai-compat", base_url="https://api.example.com",
        api_key="sk-x", default_model=model, complex_model=model,
    )


class _FakeOpenAIClient:
    """Captures the model actually used and returns a canned summary."""

    def __init__(self, sink: dict):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self._sink = sink

    async def _create(self, **kwargs):
        self._sink["model"] = kwargs.get("model")
        self._sink["calls"] = self._sink.get("calls", 0) + 1
        msg = SimpleNamespace(content="这是一段压缩摘要")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


@pytest.mark.asyncio
async def test_compress_not_triggered_below_threshold(db, monkeypatch):
    monkeypatch.setenv("MEMORY_WINDOW_SIZE", "2")
    monkeypatch.setenv("MEMORY_COMPRESSION_BATCH", "2")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import short_term_memory as stm

    # 3 rounds, window 2, batch 2 → (3-0)-2 = 1 < 2 → no fire.
    conv_id = await _make_conv_with_rounds(db, 3)
    compressed = await stm.maybe_compress("owner", conv_id, _session_llm_cfg())
    assert compressed == 0

    from src.conversations.models import Conversation

    async with db.get_session_factory()() as s:
        conv = await s.get(Conversation, conv_id)
        assert conv.compressed_count == 0
        assert not conv.context_summary


@pytest.mark.asyncio
async def test_compress_triggers_and_uses_session_llm(db, monkeypatch):
    monkeypatch.setenv("MEMORY_WINDOW_SIZE", "2")
    monkeypatch.setenv("MEMORY_COMPRESSION_BATCH", "2")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import short_term_memory as stm

    sink: dict = {}

    def _capturing_get_client(cfg=None):
        sink["cfg"] = cfg
        return _FakeOpenAIClient(sink)

    monkeypatch.setattr("src.infra.llm.get_client", _capturing_get_client)

    # 5 rounds, window 2, batch 2:
    #   pass1: (5-0)-2 = 3 >= 2 → compress rounds 1-2 (compressed_count→2)
    #   pass2: (5-2)-2 = 1 < 2 → stop. Exactly one batch.
    conv_id = await _make_conv_with_rounds(db, 5)
    session_cfg = _session_llm_cfg("sess-model")
    compressed = await stm.maybe_compress("owner", conv_id, session_cfg)

    assert compressed == 2
    # The SESSION cfg object itself reached get_client, and the model used is
    # the session model — NOT a hardcoded Haiku/vendor model (PRD 2026-07-16).
    assert sink["cfg"] is session_cfg
    assert sink["model"] == "sess-model"
    assert sink["calls"] == 1

    from src.conversations.models import Conversation

    async with db.get_session_factory()() as s:
        conv = await s.get(Conversation, conv_id)
        assert conv.compressed_count == 2
        # Summary carries the batch label + the LLM output, persisted to PG.
        assert "[对话 1-2 轮]" in conv.context_summary
        assert "这是一段压缩摘要" in conv.context_summary
        # Watermark = last message id of round 2 (0-indexed round 1 → a1).
        assert conv.compression_watermark == f"{conv_id}-a1"


class _FakeAnthropicCompressClient:
    """Anthropic-shaped capture client for the compression path."""

    def __init__(self, sink: dict):
        self._sink = sink
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self._sink["model"] = kwargs.get("model")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="A摘要")])


@pytest.mark.asyncio
async def test_compress_rounds_anthropic_path_pins_session_default_model(monkeypatch):
    """Anthropic-provider session cfg routes via messages.create with the
    session default_model — even for long batches (no silent complex-model
    escalation, no hardcoded vendor model)."""
    from src.conversations.short_term_memory import _compress_rounds
    from src.settings_user import UserLLMConfig

    sink: dict = {}
    monkeypatch.setattr(
        "src.infra.llm.get_client", lambda cfg=None: _FakeAnthropicCompressClient(sink)
    )
    cfg = UserLLMConfig(
        provider="anthropic", base_url="https://api.anthropic.com", api_key="k",
        default_model="user-default-model", complex_model="user-complex-model",
    )
    rounds = [[
        SimpleNamespace(role="user", content="长" * 3000),      # > per-msg cap
        SimpleNamespace(role="assistant", content="答" * 3000),
    ]]
    out = await _compress_rounds(rounds, 1, cfg)
    assert out == "A摘要"
    assert sink["model"] == "user-default-model"


def test_resolve_session_llm_applies_conv_model_override(monkeypatch):
    """Per-conversation llm_model override swaps default+complex via
    dataclasses.replace — mirrors app._run_chat_session's model_override."""
    from src.conversations.short_term_memory import resolve_session_llm

    base_cfg = _session_llm_cfg("base-model")
    monkeypatch.setattr("src.settings_user.resolve_user_llm", lambda user: base_cfg)

    # Override set → both models swapped.
    out = resolve_session_llm(object(), SimpleNamespace(llm_model="conv-model"))
    assert out.default_model == "conv-model"
    assert out.complex_model == "conv-model"
    assert out.provider == base_cfg.provider

    # No override → cfg passes through untouched.
    out2 = resolve_session_llm(object(), SimpleNamespace(llm_model=None))
    assert out2 is base_cfg

    # No user cfg (env-fallback user) → None, and an override alone can't
    # fabricate a cfg (same guard as app.py: override needs a base cfg).
    monkeypatch.setattr("src.settings_user.resolve_user_llm", lambda user: None)
    assert resolve_session_llm(object(), SimpleNamespace(llm_model="x")) is None


@pytest.mark.asyncio
async def test_compress_appends_batches_incrementally(db, monkeypatch):
    """Second trigger appends a new labeled block — linear concatenation,
    no re-compression of earlier summary (PRD §4.3 摘要拼法)."""
    monkeypatch.setenv("MEMORY_WINDOW_SIZE", "2")
    monkeypatch.setenv("MEMORY_COMPRESSION_BATCH", "2")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import short_term_memory as stm
    from src.conversations.models import Conversation, Message

    sink: dict = {}
    monkeypatch.setattr("src.infra.llm.get_client", lambda cfg=None: _FakeOpenAIClient(sink))

    conv_id = await _make_conv_with_rounds(db, 5)
    assert await stm.maybe_compress("owner", conv_id, _session_llm_cfg()) == 2

    # Two more rounds arrive → (7-2)-2 = 3 >= 2 → next batch (rounds 3-4).
    base = datetime(2026, 1, 2, tzinfo=timezone.utc)
    async with db.get_session_factory()() as s:
        for i in range(5, 7):
            s.add(Message(
                id=f"{conv_id}-u{i}", conversation_id=conv_id, role="user",
                content=f"问题{i}", created_at=base + timedelta(seconds=2 * i),
            ))
            s.add(Message(
                id=f"{conv_id}-a{i}", conversation_id=conv_id, role="assistant",
                content=f"回答{i}", created_at=base + timedelta(seconds=2 * i + 1),
            ))
        await s.commit()

    assert await stm.maybe_compress("owner", conv_id, _session_llm_cfg()) == 2

    async with db.get_session_factory()() as s:
        conv = await s.get(Conversation, conv_id)
        assert conv.compressed_count == 4
        # Both labeled blocks present, in chronological order.
        i1 = conv.context_summary.index("[对话 1-2 轮]")
        i2 = conv.context_summary.index("[对话 3-4 轮]")
        assert i1 < i2


@pytest.mark.asyncio
async def test_memory_update_end_to_end(db, monkeypatch):
    """_memory_update (the scheduled background job): records into the Redis
    window, and on the assistant turn runs compression + mirrors meta."""
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("MEMORY_WINDOW_SIZE", "2")
    monkeypatch.setenv("MEMORY_COMPRESSION_BATCH", "2")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import short_term_memory as stm
    from src.conversations.models import Conversation
    from src.infra.redis_client import set_redis

    rc = _fake_redis()
    set_redis(rc)
    sink: dict = {}
    monkeypatch.setattr("src.infra.llm.get_client", lambda cfg=None: _FakeOpenAIClient(sink))

    conv_id = await _make_conv_with_rounds(db, 5)  # 5 archived rounds in PG
    await stm._memory_update("owner", conv_id, "assistant", "回答4", _session_llm_cfg())

    # Redis window got the message (trimmed to window*2 = 4 max).
    assert await rc._raw.llen(stm._messages_key("owner", conv_id)) == 1
    # Compression ran (assistant turn) and mirrored into the meta hash.
    meta = await rc._raw.hgetall(stm._meta_key("owner", conv_id))
    assert "[对话 1-2 轮]" in meta["context_summary"]
    assert meta["compressed_count"] == "2"
    assert await rc._raw.ttl(stm._meta_key("owner", conv_id)) > 0

    async with db.get_session_factory()() as s:
        conv = await s.get(Conversation, conv_id)
        assert conv.compressed_count == 2
        assert "[对话 1-2 轮]" in conv.context_summary
    # And the hot read now serves the summary without touching PG.
    assert "[对话 1-2 轮]" in await stm.get_context_summary("owner", conv_id)


@pytest.mark.asyncio
async def test_compress_matches_prd_43_table(db, monkeypatch):
    """Replay the PRD §4.3 timeline: window=10, batch=5, rounds arriving one
    by one. Compression fires exactly at rounds 15 / 20 / 25 (folding 1-5,
    6-10, 11-15) and does NOT fire at 26 ("batch未满") — summary ends at
    v1+v2+v3 with compressed_count 15.
    """
    monkeypatch.setenv("MEMORY_WINDOW_SIZE", "10")
    monkeypatch.setenv("MEMORY_COMPRESSION_BATCH", "5")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import short_term_memory as stm
    from src.conversations.models import Conversation, Message

    sink: dict = {}
    monkeypatch.setattr("src.infra.llm.get_client", lambda cfg=None: _FakeOpenAIClient(sink))

    conv_id = await _make_conv_with_rounds(db, 0)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fired_at: list[int] = []
    for i in range(26):  # rounds 1..26
        async with db.get_session_factory()() as s:
            s.add(Message(
                id=f"{conv_id}-u{i}", conversation_id=conv_id, role="user",
                content=f"问题{i}", created_at=base + timedelta(seconds=2 * i),
            ))
            s.add(Message(
                id=f"{conv_id}-a{i}", conversation_id=conv_id, role="assistant",
                content=f"回答{i}", created_at=base + timedelta(seconds=2 * i + 1),
            ))
            await s.commit()
        if await stm.maybe_compress("owner", conv_id, _session_llm_cfg()) > 0:
            fired_at.append(i + 1)

    assert fired_at == [15, 20, 25]  # PRD §4.3: 压1-5 / 压6-10 / 压11-15, 26 不触发

    async with db.get_session_factory()() as s:
        conv = await s.get(Conversation, conv_id)
        assert conv.compressed_count == 15
        for lo, hi in ((1, 5), (6, 10), (11, 15)):
            assert f"[对话 {lo}-{hi} 轮]" in conv.context_summary
        assert "[对话 16-20 轮]" not in conv.context_summary


@pytest.mark.asyncio
async def test_compress_disabled_when_batch_zero(db, monkeypatch):
    """MEMORY_COMPRESSION_BATCH<=0 escape hatch → never compress."""
    monkeypatch.setenv("MEMORY_WINDOW_SIZE", "2")
    monkeypatch.setenv("MEMORY_COMPRESSION_BATCH", "0")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import short_term_memory as stm

    conv_id = await _make_conv_with_rounds(db, 20)
    assert await stm.maybe_compress("owner", conv_id, _session_llm_cfg()) == 0


@pytest.mark.asyncio
async def test_compress_disabled_when_window_zero(db, monkeypatch):
    """MEMORY_WINDOW_SIZE=0 keep-all escape hatch → never compress."""
    monkeypatch.setenv("MEMORY_WINDOW_SIZE", "0")
    monkeypatch.setenv("MEMORY_COMPRESSION_BATCH", "5")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import short_term_memory as stm

    conv_id = await _make_conv_with_rounds(db, 20)
    assert await stm.maybe_compress("owner", conv_id, _session_llm_cfg()) == 0


@pytest.mark.asyncio
async def test_compress_llm_failure_is_swallowed(db, monkeypatch):
    """LLM raising → compression skipped, bookkeeping untouched, no exception."""
    monkeypatch.setenv("MEMORY_WINDOW_SIZE", "2")
    monkeypatch.setenv("MEMORY_COMPRESSION_BATCH", "2")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import short_term_memory as stm

    def _boom(cfg=None):
        raise RuntimeError("llm exploded")

    monkeypatch.setattr("src.infra.llm.get_client", _boom)

    conv_id = await _make_conv_with_rounds(db, 5)
    compressed = await stm.maybe_compress("owner", conv_id, _session_llm_cfg())
    assert compressed == 0  # failure → nothing folded in

    from src.conversations.models import Conversation

    async with db.get_session_factory()() as s:
        conv = await s.get(Conversation, conv_id)
        assert conv.compressed_count == 0


@pytest.mark.asyncio
async def test_compress_cas_discards_stale_concurrent_write(db, monkeypatch):
    """Multi-worker safety: if another worker advances compressed_count while
    this worker is mid-LLM, the CAS commit discards the stale result — the
    winner's summary survives untouched (no duplicate batch append, no
    bookkeeping regression). PRD §4.3: duplicate LLM calls OK, duplicate
    summary concatenation NOT OK.
    """
    monkeypatch.setenv("MEMORY_WINDOW_SIZE", "2")
    monkeypatch.setenv("MEMORY_COMPRESSION_BATCH", "2")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import short_term_memory as stm
    from src.conversations.models import Conversation

    conv_id = await _make_conv_with_rounds(db, 5)

    async def _racing_compress(rounds, n, cfg):
        # Simulate a second worker committing the same batch first, in the gap
        # between this worker's snapshot read and its CAS write.
        async with db.get_session_factory()() as s:
            other = await s.get(Conversation, conv_id)
            other.compressed_count = 2
            other.context_summary = "[对话 1-2 轮] 另一个worker先写入的摘要"
            other.compression_watermark = f"{conv_id}-a1"
            await s.commit()
        return "本worker迟到的摘要"

    monkeypatch.setattr(stm, "_compress_rounds", _racing_compress)

    # Stale worker: CAS (WHERE compressed_count == 0) misses → 0 compressed.
    assert await stm.maybe_compress("owner", conv_id, _session_llm_cfg()) == 0

    async with db.get_session_factory()() as s:
        conv = await s.get(Conversation, conv_id)
        # Winner's state intact: count not regressed, batch appears exactly once.
        assert conv.compressed_count == 2
        assert conv.context_summary.count("[对话 1-2 轮]") == 1
        assert "另一个worker先写入的摘要" in conv.context_summary
        assert "本worker迟到的摘要" not in conv.context_summary
        assert conv.compression_watermark == f"{conv_id}-a1"


# --------------------------------------------------------------------------- #
# context_summary read: Redis hot → PG cold fallback + owner scoping
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_summary_pg_fallback_and_owner_scope(db, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import short_term_memory as stm
    from src.conversations.models import Conversation
    from src.infra.redis_client import set_redis

    set_redis(_fake_redis())  # empty hot cache → forces PG cold path

    conv_id = str(uuid.uuid4())
    async with db.get_session_factory()() as s:
        s.add(Conversation(
            id=conv_id, user_id="owner", title="t", kb_id=None,
            context_summary="PG 兜底摘要",
        ))
        await s.commit()

    # Owner sees the summary from PG (Redis hot miss).
    assert await stm.get_context_summary("owner", conv_id) == "PG 兜底摘要"
    # A different user id must NOT read it (owner-scoped, no leak).
    assert await stm.get_context_summary("intruder", conv_id) == ""


@pytest.mark.asyncio
async def test_get_summary_empty_when_feature_off(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    from src.settings import get_settings

    get_settings.cache_clear()
    from src.conversations import short_term_memory as stm

    assert await stm.get_context_summary("u", "c") == ""


# --------------------------------------------------------------------------- #
# _group_rounds unit
# --------------------------------------------------------------------------- #
def test_group_rounds_starts_each_round_at_user():
    from src.conversations.short_term_memory import _group_rounds

    msgs = [
        SimpleNamespace(role="user", content="q1"),
        SimpleNamespace(role="assistant", content="a1"),
        SimpleNamespace(role="user", content="q2"),
        SimpleNamespace(role="assistant", content="a2"),
    ]
    rounds = _group_rounds(msgs)
    assert len(rounds) == 2
    assert [m.content for m in rounds[0]] == ["q1", "a1"]
    assert [m.content for m in rounds[1]] == ["q2", "a2"]


# --------------------------------------------------------------------------- #
# Migration: conversations gets the three M2 columns (portable DDL)
# --------------------------------------------------------------------------- #
def test_migration_adds_conversation_memory_columns(tmp_path):
    """A legacy conversations table gains compressed_count / watermark /
    context_summary, existing rows take the DEFAULT, and re-running is a no-op.
    """
    from sqlalchemy import create_engine, inspect, text

    from src.infra.database import _migrate_additive_columns

    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE conversations ("
            "id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36), title VARCHAR(128))"
        ))
        conn.execute(text(
            "INSERT INTO conversations (id, user_id, title) VALUES ('c1', 'u1', 't')"
        ))

    for _ in range(2):  # idempotent across two "startups"
        with engine.begin() as conn:
            _migrate_additive_columns(conn)

    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("conversations")}
    assert {"compressed_count", "compression_watermark", "context_summary"} <= cols

    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT compressed_count, compression_watermark, context_summary "
            "FROM conversations WHERE id = 'c1'"
        )).one()
    assert row[0] == 0            # NOT NULL DEFAULT 0
    assert row[1] is None          # nullable
    assert row[2] is None          # nullable


# --------------------------------------------------------------------------- #
# plan_node: L4 read path + backward compat (conversation_id missing)
# --------------------------------------------------------------------------- #
class _TextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text

    def model_dump(self):
        return {"type": "text", "text": self.text}


class _FakeAnthropicClient:
    """Captures messages.create kwargs; returns a plain text reply."""

    def __init__(self, sink: dict):
        self._sink = sink
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self._sink.setdefault("kwargs", []).append(kwargs)
        return SimpleNamespace(
            content=[_TextBlock("好的")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


async def _run_plan(state: dict, monkeypatch) -> tuple[dict, dict]:
    from src.agent.nodes import plan_node
    from src.infra.llm import CostTracker
    from src.tools.base import ToolRegistry

    sink: dict = {}
    monkeypatch.setattr(
        "src.agent.nodes.get_client", lambda cfg=None: _FakeAnthropicClient(sink)
    )
    out = await plan_node(
        state,
        registry=ToolRegistry(),
        cost=CostTracker(),
        system_prompt="SYS",
        include_travel_skill=False,
    )
    return out, sink


@pytest.mark.asyncio
async def test_plan_node_without_conversation_id_is_m1(monkeypatch):
    """No conversation_id in state → no L4 layer, no error — exact M1 behavior."""
    monkeypatch.setenv("REDIS_URL", "redis://fake")  # feature on, but no session id
    from src.settings import get_settings

    get_settings.cache_clear()

    state = {"messages": [{"role": "user", "content": "你好"}], "iterations": 0}
    out, sink = await _run_plan(state, monkeypatch)

    system_blocks = sink["kwargs"][0]["system"]
    joined = " ".join(b["text"] for b in system_blocks)
    assert "早期对话摘要" not in joined
    assert joined.startswith("SYS")
    assert out["final_report"] == "好的"


@pytest.mark.asyncio
async def test_plan_node_injects_l4_from_redis_hot(monkeypatch):
    """conversation_id + user_id present and summary hot in Redis → L4 injected."""
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    from src.settings import get_settings

    get_settings.cache_clear()

    from src.conversations import short_term_memory as stm
    from src.infra.redis_client import set_redis

    rc = _fake_redis()
    set_redis(rc)
    await rc.hset(stm._meta_key("u9", "c9"), {"context_summary": "[对话 1-5 轮] 早期结论XYZ"})

    state = {
        "messages": [{"role": "user", "content": "继续"}],
        "iterations": 0,
        "conversation_id": "c9",
        "user_id": "u9",
    }
    out, sink = await _run_plan(state, monkeypatch)

    joined = " ".join(b["text"] for b in sink["kwargs"][0]["system"])
    assert "早期对话摘要" in joined
    assert "早期结论XYZ" in joined
    assert out["final_report"] == "好的"


@pytest.mark.asyncio
async def test_plan_node_summary_read_failure_degrades(monkeypatch):
    """get_context_summary blowing up must not break planning (L4 just omitted)."""
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    from src.settings import get_settings

    get_settings.cache_clear()

    async def _boom(user_id, conv_id, session=None):
        raise RuntimeError("memory backend down")

    monkeypatch.setattr(
        "src.conversations.short_term_memory.get_context_summary", _boom
    )

    state = {
        "messages": [{"role": "user", "content": "hi"}],
        "iterations": 0,
        "conversation_id": "c1",
        "user_id": "u1",
    }
    out, sink = await _run_plan(state, monkeypatch)
    joined = " ".join(b["text"] for b in sink["kwargs"][0]["system"])
    assert "早期对话摘要" not in joined
    assert out["final_report"] == "好的"
    # Failure result is cached as "" so later iterations won't re-hit the backend.
    assert out["early_summary"] == ""


@pytest.mark.asyncio
async def test_plan_node_fetches_summary_once_per_request(monkeypatch):
    """The L4 read is per-request, not per-iteration: the first plan_node call
    fetches once and caches into state; a call with the cache present must not
    touch the memory backend again (MAX_ITERATIONS=10 → at most 1 read, not 10).
    """
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    from src.settings import get_settings

    get_settings.cache_clear()

    calls = {"n": 0}

    async def _counting(user_id, conv_id, session=None):
        calls["n"] += 1
        return "[对话 1-5 轮] 缓存验证摘要"

    monkeypatch.setattr(
        "src.conversations.short_term_memory.get_context_summary", _counting
    )

    state = {
        "messages": [{"role": "user", "content": "第一轮"}],
        "iterations": 0,
        "conversation_id": "c1",
        "user_id": "u1",
    }
    out, sink = await _run_plan(state, monkeypatch)
    assert calls["n"] == 1
    assert out["early_summary"] == "[对话 1-5 轮] 缓存验证摘要"

    # Simulate the next plan iteration of the SAME request (state carried over).
    state2 = {**out, "final_report": None, "pending_tool_calls": []}
    out2, sink2 = await _run_plan(state2, monkeypatch)
    assert calls["n"] == 1  # cached — no second backend read
    joined = " ".join(b["text"] for b in sink2["kwargs"][0]["system"])
    assert "缓存验证摘要" in joined  # L4 still injected from the cache

@pytest.mark.asyncio
async def test_append_message_still_works_with_memory_off(client, create_user):
    """REDIS_URL unset (conftest default) → append behaves exactly as M1:
    the message persists and the response is unchanged (no error from the
    fire-and-forget hook, which short-circuits when the feature is off).
    """
    from src.auth.tokens import issue_token

    u = await create_user("mem@x.com")
    token = issue_token(u.id, u.email)
    headers = {"Authorization": f"Bearer {token}"}

    conv = (await client.post("/api/conversations", json={}, headers=headers)).json()
    r = await client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"role": "user", "content": "你好"},
        headers=headers,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "user"
    assert body["content"] == "你好"

