"""Short-term memory: Redis hot-storage window + batch compression (v3-M2).

Implements PRD §4 (memory-optimization). The write path hangs off
``POST /conversations/{id}/messages`` (``routes.append_message``):

    append_message → record_message (LPUSH + LTRIM window + refresh TTL)
                   → schedule maybe_compress (async, non-blocking):
                       uncompressed rounds ≥ BATCH and past the window?
                         → compress oldest BATCH rounds with the SESSION LLM
                         → append to context_summary
                         → persist to PG conversations + mirror to Redis Hash

The read path (``get_context_summary``) is consumed by ``plan_node`` as the L4
"early summary" layer, so information from messages that slid out of the L5
window survives.

Degradation (PRD §11):
  * ``REDIS_URL`` empty → feature off entirely (``short_term_memory_enabled`` is
    False); the route never schedules anything → behavior is exactly M1.
  * ``REDIS_URL`` set but Redis down → every Redis op no-ops, but compression
    still writes to PG and ``get_context_summary`` falls back to PG, so L4 keeps
    working. Chat is never blocked or 500'd.
  * ``memory_window_size == 0`` or ``memory_compression_batch <= 0`` → no
    compression (PRD §11 escape hatch).

Compression LLM (PRD 2026-07-16 decision): always the user's per-session LLM
(``resolve_user_llm`` + per-conversation model override), never a hardcoded
vendor/model. Failures are logged and skipped — never surfaced to chat.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace as dc_replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from src.infra.redis_client import get_redis
from src.settings import get_settings

if TYPE_CHECKING:
    from src.settings_user import UserLLMConfig

log = logging.getLogger(__name__)

# TTLs per PRD §4.2. Messages are a hot cache (PG is the durable archive);
# meta (summary / bookkeeping) lives longer because PG mirrors it too.
_MESSAGES_TTL = 259_200   # 3 days
_META_TTL = 604_800       # 7 days

# Keep background compression tasks referenced so they aren't GC'd mid-flight,
# and serialize per-conversation compression to avoid a double-compress race
# when the user + assistant messages of a turn schedule overlapping checks.
_bg_tasks: set[asyncio.Task] = set()
_conv_locks: dict[str, asyncio.Lock] = {}


# ---------------------------------------------------------------------------
# Keys + feature gate
# ---------------------------------------------------------------------------
def _messages_key(user_id: str, conv_id: str) -> str:
    return f"user_memory:{user_id}:{conv_id}:messages"


def _meta_key(user_id: str, conv_id: str) -> str:
    return f"user_memory:{user_id}:{conv_id}:meta"


def short_term_memory_enabled() -> bool:
    """True when the M2 feature is switched on (REDIS_URL configured).

    Empty REDIS_URL keeps the whole path dormant → identical to M1. When set,
    the path runs even if Redis is momentarily down (PG fallback covers L4).
    """
    return bool(get_settings().redis_url)


# ---------------------------------------------------------------------------
# Write path — Redis hot window
# ---------------------------------------------------------------------------
async def record_message(user_id: str, conv_id: str, role: str, content: str) -> None:
    """LPUSH one message into the Redis window, LTRIM to size, refresh TTL.

    No-op when the feature is off or Redis is unavailable (degrades silently).
    Window holds ``memory_window_size * 2`` newest messages (rounds × 2);
    window_size 0 disables trimming (keep-all escape hatch, PRD §11).
    """
    if not short_term_memory_enabled():
        return
    redis = get_redis()
    if not redis.enabled:
        return

    s = get_settings()
    key = _messages_key(user_id, conv_id)
    payload = json.dumps(
        {"role": role, "content": content or "", "ts": int(time.time())},
        ensure_ascii=False,
    )
    # LPUSH prepends (index 0 = newest); LTRIM 0 N-1 keeps the N newest.
    await redis.lpush(key, payload)
    if s.memory_window_size > 0:
        await redis.ltrim(key, 0, s.memory_window_size * 2 - 1)
    await redis.expire(key, _MESSAGES_TTL)


async def read_window_messages(user_id: str, conv_id: str) -> list[dict[str, Any]]:
    """Read the Redis message window oldest→newest (hot cache; may be empty).

    Not wired into plan_node context in M2 (the frontend still owns the message
    list — architecture tension #1); provided for tests + future cross-device
    reuse. Returns [] when the feature is off / Redis down / cache cold.
    """
    if not short_term_memory_enabled():
        return []
    redis = get_redis()
    raw = await redis.lrange(_messages_key(user_id, conv_id), 0, -1)
    if not raw:
        return []
    out: list[dict[str, Any]] = []
    for item in reversed(raw):  # LRANGE returns newest→oldest; flip to chrono.
        try:
            out.append(json.loads(item))
        except (ValueError, TypeError):
            continue
    return out


# ---------------------------------------------------------------------------
# context_summary (L4) — Redis Hash meta (hot) + PG (durable)
# ---------------------------------------------------------------------------
async def get_context_summary(user_id: str, conv_id: str, session: Any = None) -> str:
    """Return the accumulated early-history summary for L4 injection.

    Reads Redis Hash meta first (hot), falls back to the PG conversations row
    (durable, survives Redis TTL / outage). Returns "" when nothing has been
    compressed yet or on any failure — callers then simply omit the L4 layer.
    """
    if not short_term_memory_enabled():
        return ""

    redis = get_redis()
    hot = await redis.hget(_meta_key(user_id, conv_id), "context_summary")
    if hot:
        return hot

    # Cold path: fall back to PG. Accept a caller-provided session or open one.
    try:
        if session is not None:
            summary = await _read_summary_pg(session, user_id, conv_id)
        else:
            from src.infra.database import get_session_factory

            async with get_session_factory()() as own:
                summary = await _read_summary_pg(own, user_id, conv_id)
    except Exception as exc:  # noqa: BLE001 — L4 is best-effort, never blocks chat.
        log.warning("context_summary_pg_read_failed conv=%s error=%s", conv_id, exc)
        return ""
    return summary or ""


async def _read_summary_pg(session: Any, user_id: str, conv_id: str) -> str:
    """Owner-scoped summary read — a foreign conversation id yields ""."""
    from src.conversations.models import Conversation

    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.user_id != user_id:
        return ""
    return conv.context_summary or ""


async def clear_conversation_hot_state(user_id: str, conv_id: str) -> None:
    """Drop a conversation's Redis hot keys (window + meta) — called on finalize.

    Best-effort (mirrors ``invalidate_profile_cache``): no-op when the feature is
    off, and swallows any Redis error. Only the hot cache is cleared — the
    durable PG copy (messages archive + ``context_summary``) is untouched, so a
    later read simply re-warms from PG. Keys are namespaced by the authenticated
    uid, so a forged conv id can only clear that user's own (nonexistent) keys.
    """
    if not short_term_memory_enabled():
        return
    try:
        await get_redis().delete(
            _messages_key(user_id, conv_id), _meta_key(user_id, conv_id)
        )
    except Exception:  # noqa: BLE001 — hot-key cleanup is best-effort.
        pass


# ---------------------------------------------------------------------------
# Orchestration — called from routes.append_message
# ---------------------------------------------------------------------------
def schedule_memory_update(
    user_id: str,
    conv_id: str,
    role: str,
    content: str,
    llm_cfg: "UserLLMConfig | None",
) -> None:
    """Fire-and-forget the short-term memory update after a message append.

    Records the message into the Redis window and, on assistant turns (round
    boundary), checks whether the oldest uncompressed rounds should be
    compressed. Runs entirely in the background so the HTTP response is never
    blocked (PRD §4.3). No-op when the feature is off.
    """
    if not short_term_memory_enabled():
        return
    task = asyncio.create_task(
        _memory_update(user_id, conv_id, role, content, llm_cfg)
    )
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _memory_update(
    user_id: str,
    conv_id: str,
    role: str,
    content: str,
    llm_cfg: "UserLLMConfig | None",
) -> None:
    try:
        await record_message(user_id, conv_id, role, content)
        # Only run the (LLM-backed) compression check on assistant turns: the
        # round is complete and this halves overlap between the user+assistant
        # appends of one turn. A per-conversation lock removes the remaining race.
        if role == "assistant":
            # Bound the lock map (long-lived process hygiene). Note an
            # asyncio.Lock briefly reports locked()==False between release and
            # waiter wake-up, so eviction can in theory split waiters onto two
            # lock objects; that only risks a duplicate LLM call — the CAS in
            # maybe_compress guarantees the summary is never appended twice.
            if len(_conv_locks) > 4096:
                for k in [k for k, v in _conv_locks.items() if not v.locked()]:
                    _conv_locks.pop(k, None)
            lock = _conv_locks.setdefault(conv_id, asyncio.Lock())
            async with lock:
                await maybe_compress(user_id, conv_id, llm_cfg)
    except Exception as exc:  # noqa: BLE001 — background best-effort, never raise.
        log.warning("memory_update_failed conv=%s error=%s", conv_id, exc)


# ---------------------------------------------------------------------------
# Batch compression
# ---------------------------------------------------------------------------
def _group_rounds(messages: list[Any]) -> list[list[Any]]:
    """Group ordered messages into rounds (a round starts at each user message).

    Leading assistant messages (rare) form their own round so nothing is lost.
    """
    rounds: list[list[Any]] = []
    current: list[Any] = []
    for m in messages:
        if m.role == "user" and current:
            rounds.append(current)
            current = [m]
        else:
            current.append(m)
    if current:
        rounds.append(current)
    return rounds


async def maybe_compress(
    user_id: str,
    conv_id: str,
    llm_cfg: "UserLLMConfig | None",
) -> int:
    """Compress oldest uncompressed rounds past the window into context_summary.

    Trigger (PRD §4.3 diagram): the number of uncompressed rounds that have
    slid OUT of the L5 window must reach a full batch —
    ``(total_rounds - compressed_count) - window_size >= batch``.
    E.g. window=10 / batch=5: fires at rounds 15, 20, 25 (compressing 1-5,
    6-10, 11-15); at round 26 only 1 round is beyond the window → "batch未满",
    no fire. This keeps L4 (summary) and L5 (window) perfectly complementary:
    a round is summarized only once it can no longer appear verbatim in L5.

    Compresses BATCH rounds per iteration and loops while still triggered
    (catches up after bulk imports). Returns the number of newly-compressed
    rounds (0 when nothing fired). Uses its own DB sessions — safe to run
    after the request session is closed.

    Concurrency (v3-M2): the in-process per-conversation lock in
    ``_memory_update`` serializes same-worker runs; across workers/processes
    the final write is a compare-and-swap on ``compressed_count``, so a stale
    run is discarded whole — the same batch can never be appended twice to
    ``context_summary`` and a slower worker can never regress the bookkeeping
    (an occasional duplicate LLM call is the accepted cost, PRD §4.3).
    """
    s = get_settings()
    window = s.memory_window_size
    batch = s.memory_compression_batch
    # Escape hatches: window_size 0 keeps everything, batch<=0 disables compression.
    if window <= 0 or batch <= 0:
        return 0

    from src.conversations.models import Conversation, Message
    from src.infra.database import get_session_factory

    factory = get_session_factory()

    # Phase 1: read a consistent snapshot (summary + count committed together),
    # then release the session so no DB transaction stays open across the slow
    # LLM calls below (SQLite would hold a read lock for seconds otherwise).
    async with factory() as session:
        conv = await session.get(Conversation, conv_id)
        if conv is None:
            return 0
        result = await session.execute(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.created_at)
        )
        rounds = _group_rounds(list(result.scalars().all()))
        summary = conv.context_summary or ""
        compressed_count = conv.compressed_count or 0
        watermark = conv.compression_watermark

    total_rounds = len(rounds)
    original_count = compressed_count
    compressed_total = 0

    while (total_rounds - compressed_count) - window >= batch:
        batch_rounds = rounds[compressed_count : compressed_count + batch]
        if not batch_rounds:
            break
        piece = await _compress_rounds(batch_rounds, len(batch_rounds), llm_cfg)
        if not piece:
            # LLM failed — stop, keep bookkeeping intact so we retry later.
            break
        lo = compressed_count + 1
        hi = compressed_count + len(batch_rounds)
        label = f"[对话 {lo}-{hi} 轮] {piece}"
        summary = f"{summary}\n{label}".strip() if summary else label
        compressed_count += len(batch_rounds)
        # Watermark = last message id of the batch we just folded in.
        last_msgs = batch_rounds[-1]
        if last_msgs:
            watermark = last_msgs[-1].id
        compressed_total += len(batch_rounds)

    if compressed_total == 0:
        return 0

    # Phase 2: compare-and-swap commit. Guarding on the snapshot's
    # compressed_count makes the write safe across workers: if another
    # worker/process advanced the bookkeeping meanwhile, this stale result is
    # dropped entirely instead of duplicating batches into context_summary or
    # regressing compressed_count/watermark. No rounds are lost — the next
    # trigger recomputes from the winner's committed state.
    async with factory() as session:
        res = await session.execute(
            update(Conversation)
            .where(
                Conversation.id == conv_id,
                Conversation.compressed_count == original_count,
            )
            .values(
                context_summary=summary,
                compressed_count=compressed_count,
                compression_watermark=watermark,
            )
        )
        await session.commit()
        if res.rowcount == 0:
            log.info(
                "compression_cas_skipped conv=%s original_count=%s (another worker advanced)",
                conv_id,
                original_count,
            )
            return 0

    # Mirror the durable state into the Redis Hash meta (hot copy).
    await _write_meta(user_id, conv_id, summary, compressed_count)
    return compressed_total


async def _write_meta(
    user_id: str, conv_id: str, summary: str, compressed_count: int
) -> None:
    """Mirror compression bookkeeping into Redis Hash meta (best-effort)."""
    redis = get_redis()
    if not redis.enabled:
        return
    key = _meta_key(user_id, conv_id)
    await redis.hset(
        key,
        {
            "context_summary": summary,
            "compressed_count": str(compressed_count),
            "last_active": str(int(time.time())),
        },
    )
    await redis.expire(key, _META_TTL)


# Bound the compression prompt so one giant archived message can't produce an
# oversized (and costly) LLM call on the user's own key. Per-message and
# whole-batch character caps; truncation is marked so the LLM knows.
_MAX_MSG_CHARS = 2000
_MAX_BATCH_CHARS = 16000


def _round_to_text(round_msgs: list[Any]) -> str:
    """Render one round's messages as ``User: ...`` / ``Assistant: ...`` lines."""
    lines: list[str] = []
    for m in round_msgs:
        speaker = "User" if m.role == "user" else "Assistant"
        text = (m.content or "").strip()
        if len(text) > _MAX_MSG_CHARS:
            text = text[:_MAX_MSG_CHARS] + "…（截断）"
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


async def _compress_rounds(
    rounds: list[list[Any]],
    round_count: int,
    llm_cfg: "UserLLMConfig | None",
) -> str:
    """Summarize a batch of rounds into <=150 Chinese chars via the SESSION LLM.

    Reuses the exact ``get_client`` / provider-routing path as plan_node /
    skills.loader — NO hardcoded vendor or model (PRD 2026-07-16 decision).
    Single turn, no tools. Returns "" on any failure (caller skips + retries).
    """
    from src.infra.llm import get_client, with_cache_control

    convo = "\n\n".join(_round_to_text(r) for r in rounds if _round_to_text(r))
    if not convo.strip():
        return ""
    if len(convo) > _MAX_BATCH_CHARS:
        convo = convo[:_MAX_BATCH_CHARS] + "…（截断）"

    user_prompt = (
        f"请将以下 {round_count} 轮对话总结成 150 字以内的中文摘要，"
        "只保留关键信息（用户问题、重要结论、上下文线索）。\n\n"
        f"{convo}\n\n"
        "摘要（150 字以内）："
    )
    system_msg = "你是对话摘要助手，只输出简洁的中文摘要，不要复述原文、不要解释你在做什么。"

    try:
        s = get_settings()
        client = get_client(llm_cfg)
        # Session default_model, deterministically — the user's own model (with
        # any per-conversation override already applied by resolve_session_llm),
        # never a hardcoded vendor model. We don't route through pick_model here
        # because its >2000-char escalation would silently bill the user's
        # complex_model for a simple summarization task.
        model = llm_cfg.default_model if llm_cfg is not None else s.llm_default_model

        if llm_cfg is not None:
            is_anthropic = llm_cfg.provider == "anthropic"
        else:
            is_anthropic = s.llm_provider == "anthropic"

        if not is_anthropic:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=400,
            )
            return (resp.choices[0].message.content or "").strip()

        resp = await client.messages.create(
            model=model,
            max_tokens=400,
            system=with_cache_control([{"type": "text", "text": system_msg}], llm_cfg),
            messages=[{"role": "user", "content": user_prompt}],
        )
        for block in resp.content or []:
            if getattr(block, "type", "") == "text":
                return (block.text or "").strip()
        return ""
    except Exception as exc:  # noqa: BLE001 — compression must never break chat.
        log.warning("compression_llm_failed error=%s", exc)
        return ""


def resolve_session_llm(user: Any, conv: Any) -> "UserLLMConfig | None":
    """Resolve the per-session LLM cfg for compression.

    Mirrors ``app._run_chat_session``: user-level cfg (BYOK), then the
    per-conversation ``llm_model`` override applied via ``dataclasses.replace``.
    Returns None (env fallback) when the user hasn't configured a BYOK LLM.
    """
    from src.settings_user import resolve_user_llm

    llm_cfg = resolve_user_llm(user) if user is not None else None
    model_override = getattr(conv, "llm_model", None)
    if model_override and llm_cfg is not None:
        llm_cfg = dc_replace(
            llm_cfg, default_model=model_override, complex_model=model_override
        )
    return llm_cfg
