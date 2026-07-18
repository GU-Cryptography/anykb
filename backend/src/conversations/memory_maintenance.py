"""Nightly memory maintenance + 24h-idle conversation scan (v3-M5, PRD §5.3/§5.6).

The closing piece of the memory-optimization work. A single in-process asyncio
loop wakes once a day (``memory_maintenance_hour``, UTC) and runs one round:

  1. ``scan_stale_conversations`` — finalize + extract long-term memories from
     conversations the user never explicitly ended (idle > 24h). Each conversation
     is claimed with the SAME compare-and-swap gate as ``POST /finalize``
     (``UPDATE ... WHERE finalized_at IS NULL``), so a background scan and a
     concurrent manual finalize — or two app instances — extract exactly once.
  2. ``maintain_user_memories`` for every user with active memories —
     vector dedup → importance decay → eviction of low-value rows (PRD §5.6).

Multi-instance safety is structural, never a single-process assumption
(background-tasks-guidelines): every mutation is an idempotent DB predicate or a
CAS. The decay UPDATE carries a ``last_decayed_at`` guard so two runs the same
night (or two instances) decay a row at most once; dedup / eviction re-check
``deleted_at IS NULL`` so a row already culled by a peer is skipped.

Degradation (error-handling.md): the whole feature is optional. Every step is
try/except-wrapped and logged; one user's failure never aborts the round, and a
vector-backend / embedding outage simply skips dedup (no O(n²) PG substitute —
task contract). The loop itself is only started when
``memory_maintenance_enabled`` is True (cleanest off-switch); within a round the
stale-conversation scan is additionally gated on ``memory_auto_extract`` (it
creates new memories — the two knobs are orthogonal).

No LLM runs in ``maintain_user_memories`` at all (dedup is pure vector; decay /
eviction are pure SQL), so there is no read→LLM→write window here; the only LLM
call in the round is inside ``extract_conversation_memories`` (reused from M3,
session-LLM + BYOK, self-contained for background use).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import and_, or_, select, update

from src.settings import get_settings

if TYPE_CHECKING:
    from src.settings_user import UserEmbeddingConfig

log = logging.getLogger(__name__)

# Strong ref to the running loop task so it can't be GC'd mid-flight
# (background-tasks-guidelines). Set by start_memory_maintenance, cleared by stop.
_maintenance_task: asyncio.Task | None = None

# --- tunables (PRD §5.6) ---
_DEDUP_THRESHOLD = 0.85     # cosine above which two memories are "the same"
_DEDUP_NEIGHBOR_LIMIT = 50  # ANN neighbors fetched per memory when clustering
# Per-user, per-round ceiling on dedup cluster leaders. Dedup re-embeds each
# still-alive memory on the user's own BYOK key (one embedding API call per row,
# every night), so an unbounded scan would bill a memory-heavy user O(n) calls
# nightly. Capping the leaders to the top-N-by-importance rows bounds that cost;
# lower-importance duplicates beyond the cap are still culled *as neighbors* of a
# leader this round, or promoted to leaders once higher ones are removed — so the
# set converges over successive nights instead of paying the full O(n) each time.
_DEDUP_MAX_LEADERS = 200
_DECAY_FACTOR = 0.9         # importance *= this per decay pass
_DECAY_AFTER_DAYS = 30      # only decay memories untouched for this long
_DECAY_COOLDOWN_HOURS = 20  # idempotency gate: re-decay only after this gap
_EVICT_BELOW = 0.3          # importance under this → evicted (soft-deleted)
_STALE_AFTER_HOURS = 24     # a conversation idle this long is auto-finalized
_STALE_SCAN_LIMIT = 20      # conversations processed per round (anti-avalanche)

# ``explicit`` memories are what the user *asked* to be remembered ("帮我记住…").
# They are exempt from BOTH decay and eviction — an explicit request shouldn't
# quietly fade or be culled by the automated janitor (design decision, PRD §5.4
# marks explicit as user-driven). Dedup still applies (a literal duplicate of an
# explicit memory is still redundant).
_PROTECTED_TYPE = "explicit"


def _utcnow() -> datetime:
    """Single now() seam (tests inject a fixed time without sleeping real secs)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Per-user maintenance: dedup → decay → eviction (each step isolated)
# ---------------------------------------------------------------------------
async def maintain_user_memories(user_id: str) -> dict[str, int]:
    """Run the three maintenance steps for one user (never raises).

    Returns a small counters dict ({"deduped", "decayed", "evicted"}) for
    logging / tests. Each step commits independently and is wrapped so a failure
    is logged and the next step still runs; the profile hot cache is dropped once
    at the end since any cull / decay can shift the aggregated L1 profile.
    """
    if not user_id:
        return {"deduped": 0, "decayed": 0, "evicted": 0}

    counters = {"deduped": 0, "decayed": 0, "evicted": 0}
    changed = False

    # Step 1 — dedup (pure vector, zero LLM). Skipped wholesale when the user has
    # no embedding cfg or the backend can't do collections (degrade, no O(n²) PG).
    try:
        counters["deduped"] = await _dedup_user_memories(user_id)
        changed = changed or counters["deduped"] > 0
    except Exception as exc:  # noqa: BLE001 — one step failing must not skip the rest.
        log.warning("memory_dedup_failed user=%s error=%s", user_id, exc)

    # Step 2 — importance decay (pure SQL, idempotent via last_decayed_at gate).
    try:
        counters["decayed"] = await _decay_user_memories(user_id)
        changed = changed or counters["decayed"] > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("memory_decay_failed user=%s error=%s", user_id, exc)

    # Step 3 — eviction of now-low-value rows (soft-delete + vector hard-delete).
    try:
        counters["evicted"] = await _evict_user_memories(user_id)
        changed = changed or counters["evicted"] > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("memory_evict_failed user=%s error=%s", user_id, exc)

    if changed:
        try:
            from src.conversations.long_term_memory import invalidate_profile_cache

            await invalidate_profile_cache(user_id)
        except Exception as exc:  # noqa: BLE001 — cache bust is best-effort.
            log.warning("memory_maintain_invalidate_failed user=%s error=%s", user_id, exc)

    return counters


async def _load_active_memories(user_id: str, limit: int | None = None) -> list:
    """Active (non-soft-deleted) memories for a user, importance DESC then newest.

    That ordering makes dedup's greedy pass correct: when a memory is reached
    still-alive, it is the highest-priority survivor of its similarity cluster
    (highest importance, newest on ties), so it wins and its neighbors are culled.
    ``limit`` caps the returned leaders (dedup passes ``_DEDUP_MAX_LEADERS`` to
    bound the nightly per-user embedding cost). Returns lightweight snapshots
    (id/content/importance) — no ORM instances escape the session
    (background-tasks-guidelines).
    """
    from types import SimpleNamespace

    from src.conversations.models import UserMemory
    from src.infra.database import get_session_factory

    async with get_session_factory()() as session:
        stmt = (
            select(UserMemory)
            .where(
                UserMemory.user_id == user_id,
                UserMemory.deleted_at.is_(None),
            )
            .order_by(UserMemory.importance.desc(), UserMemory.created_at.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
    return [
        SimpleNamespace(id=r.id, content=r.content or "", importance=r.importance)
        for r in rows
    ]


async def _dedup_user_memories(user_id: str) -> int:
    """Soft-delete near-duplicate memories (cosine > 0.85), keeping the best one.

    Pure vector: only runs when the backend supports the memory collection AND
    the user has an embedding cfg (same seam write/recall use). Each still-alive
    memory (processed importance-first) is re-embedded and its same-user ANN
    neighbors fetched with scores; neighbors above the threshold are the cluster's
    losers → soft-deleted + vector hard-deleted. No capability / no embedding →
    return 0 (degrade; NO O(n²) PG fallback, per task contract).
    """
    from src.conversations.long_term_memory import _resolve_user_embedding_by_id
    from src.infra.memory_vector import (
        search_memory_vectors_scored,
        vector_capability_available,
    )

    if not vector_capability_available():
        return 0
    embedding_cfg = await _resolve_user_embedding_by_id(user_id)
    if embedding_cfg is None:
        return 0

    active = await _load_active_memories(user_id, limit=_DEDUP_MAX_LEADERS)
    if len(active) < 2:
        return 0

    removed: set[str] = set()
    losers: list[str] = []
    for m in active:
        if m.id in removed or not m.content.strip():
            continue
        try:
            vec = await _embed_memory(m.content, embedding_cfg)
        except Exception as exc:  # noqa: BLE001 — one bad embed shouldn't stop dedup.
            log.warning("memory_dedup_embed_failed memory=%s error=%s", m.id, exc)
            continue
        if not vec:
            continue
        neighbors = await search_memory_vectors_scored(
            vec, user_id, _DEDUP_NEIGHBOR_LIMIT
        )
        for nid, score in neighbors:
            if nid == m.id or nid in removed:
                continue
            if score > _DEDUP_THRESHOLD:
                # m is the highest-priority survivor of the cluster (active is
                # importance-sorted and m is unremoved), so every strong neighbor
                # is a loser. Tie-break (importance + recency) is already encoded
                # in the sort order.
                removed.add(nid)
                losers.append(nid)

    if not losers:
        return 0
    return await _soft_delete_with_vectors(user_id, losers)


async def _embed_memory(content: str, cfg: "UserEmbeddingConfig") -> list[float]:
    """Embed one memory's content through the shared seam (patchable in tests)."""
    from src.infra.embedding import embed

    return await embed(content, cfg=cfg)


async def _soft_delete_with_vectors(user_id: str, ids: list[str]) -> int:
    """Soft-delete owner-scoped rows (deleted_at gate) + hard-delete their vectors.

    The ``deleted_at IS NULL`` predicate makes this idempotent and multi-instance
    safe: a row a peer already culled is not re-counted. The vector is hard-deleted
    (best-effort — the helper swallows its own errors) since the ANN index carries
    no soft-delete state. Returns the number of rows this call actually culled.
    """
    from src.conversations.models import UserMemory
    from src.infra.database import get_session_factory
    from src.infra.memory_vector import delete_memory_vector

    if not ids:
        return 0
    now = _utcnow()
    async with get_session_factory()() as session:
        res = await session.execute(
            update(UserMemory)
            .where(
                UserMemory.user_id == user_id,
                UserMemory.id.in_(ids),
                UserMemory.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )
        await session.commit()
    for mid in ids:
        await delete_memory_vector(mid)  # best-effort, never raises
    return res.rowcount or 0


async def _decay_user_memories(user_id: str) -> int:
    """Multiply importance by 0.9 for stale memories, once per cooldown window.

    Predicates (all owner-scoped, active, non-explicit):
      * untouched for 30d — never accessed and created > 30d ago, OR last accessed
        > 30d ago;
      * ``last_decayed_at`` NULL or older than the cooldown — the idempotency gate
        that makes a same-night double run (or two instances) decay at most once.
    A single atomic UPDATE, so correctness needs no lock. Returns rows decayed.
    """
    from src.conversations.models import UserMemory
    from src.infra.database import get_session_factory

    now = _utcnow()
    stale_before = now - timedelta(days=_DECAY_AFTER_DAYS)
    cooldown_before = now - timedelta(hours=_DECAY_COOLDOWN_HOURS)

    async with get_session_factory()() as session:
        res = await session.execute(
            update(UserMemory)
            .where(
                UserMemory.user_id == user_id,
                UserMemory.deleted_at.is_(None),
                UserMemory.memory_type != _PROTECTED_TYPE,
                or_(
                    and_(
                        UserMemory.last_accessed_at.is_(None),
                        UserMemory.created_at < stale_before,
                    ),
                    UserMemory.last_accessed_at < stale_before,
                ),
                or_(
                    UserMemory.last_decayed_at.is_(None),
                    UserMemory.last_decayed_at < cooldown_before,
                ),
            )
            .values(
                importance=UserMemory.importance * _DECAY_FACTOR,
                last_decayed_at=now,
            )
        )
        await session.commit()
    return res.rowcount or 0


async def _evict_user_memories(user_id: str) -> int:
    """Soft-delete memories whose importance fell below the floor (0.3).

    Selects the doomed ids first (active, non-explicit, importance < floor), then
    soft-deletes + drops their vectors through the shared helper (deleted_at gate →
    idempotent / multi-instance safe). Runs after decay in the same round so a row
    pushed under the floor this night is evicted the same night.
    """
    from src.conversations.models import UserMemory
    from src.infra.database import get_session_factory

    async with get_session_factory()() as session:
        doomed = (
            (
                await session.execute(
                    select(UserMemory.id).where(
                        UserMemory.user_id == user_id,
                        UserMemory.deleted_at.is_(None),
                        UserMemory.memory_type != _PROTECTED_TYPE,
                        UserMemory.importance < _EVICT_BELOW,
                    )
                )
            )
            .scalars()
            .all()
        )
    if not doomed:
        return 0
    return await _soft_delete_with_vectors(user_id, list(doomed))


# ---------------------------------------------------------------------------
# 24h-idle conversation scan → CAS finalize + extract (PRD §5.5 auto path)
# ---------------------------------------------------------------------------
async def scan_stale_conversations() -> int:
    """Finalize + extract memories from conversations idle > 24h (never raises).

    Selects up to ``_STALE_SCAN_LIMIT`` open conversations whose ``updated_at`` is
    older than the threshold (anti-avalanche / BYOK-billing cap), then claims each
    with the SAME atomic CAS as ``POST /finalize`` (``UPDATE ... WHERE
    finalized_at IS NULL``). Only the ``rowcount == 1`` winner runs extraction, so
    a concurrent manual finalize or a second instance can never double-extract.
    Per-conversation try/except: one bad extraction doesn't abort the scan.
    Returns the number of conversations extracted this call.
    """
    from src.conversations.models import Conversation
    from src.infra.database import get_session_factory

    now = _utcnow()
    stale_before = now - timedelta(hours=_STALE_AFTER_HOURS)

    factory = get_session_factory()
    # Phase 1: snapshot candidate (id, user_id) pairs, then release the session
    # before the per-conversation extraction (which owns its own sessions + LLM).
    async with factory() as session:
        rows = (
            await session.execute(
                select(Conversation.id, Conversation.user_id)
                .where(
                    Conversation.finalized_at.is_(None),
                    Conversation.updated_at < stale_before,
                )
                .order_by(Conversation.updated_at)
                .limit(_STALE_SCAN_LIMIT)
            )
        ).all()

    candidates = [(r[0], r[1]) for r in rows]
    if not candidates:
        return 0

    from src.conversations.long_term_memory import extract_conversation_memories

    extracted = 0
    for conv_id, user_id in candidates:
        try:
            # CAS claim: flip finalized_at NULL→now for exactly one caller.
            async with factory() as session:
                res = await session.execute(
                    update(Conversation)
                    .where(
                        Conversation.id == conv_id,
                        Conversation.finalized_at.is_(None),
                    )
                    .values(finalized_at=_utcnow())
                )
                await session.commit()
            if (res.rowcount or 0) != 1:
                continue  # a peer / manual finalize won the race → don't re-extract
            # We own it. extract_conversation_memories resolves the session LLM
            # from ids and self-gates on memory_auto_extract (returns 0 when off).
            n = await extract_conversation_memories(conv_id, user_id)
            if n:
                extracted += 1
        except Exception as exc:  # noqa: BLE001 — one conversation failing != abort scan.
            log.warning("stale_scan_conv_failed conv=%s error=%s", conv_id, exc)

    return extracted


# ---------------------------------------------------------------------------
# Round orchestration + scheduler loop
# ---------------------------------------------------------------------------
async def _users_with_active_memories() -> list[str]:
    """Distinct user_ids that still own at least one active memory."""
    from src.conversations.models import UserMemory
    from src.infra.database import get_session_factory

    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(UserMemory.user_id)
                .where(UserMemory.deleted_at.is_(None))
                .distinct()
            )
        ).all()
    return [r[0] for r in rows]


async def run_maintenance_round() -> dict[str, int]:
    """One full maintenance round (directly testable; no sleeping).

    Order per PRD §7 / task: scan idle conversations FIRST (may add memories),
    then maintain every user's memory set. The scan is gated on
    ``memory_auto_extract`` (it creates memories); dedup/decay/eviction always run
    (they only prune existing rows). Never raises — each user is isolated.
    """
    summary = {"extracted": 0, "users": 0}

    if get_settings().memory_auto_extract:
        try:
            summary["extracted"] = await scan_stale_conversations()
        except Exception as exc:  # noqa: BLE001
            log.warning("stale_scan_round_failed error=%s", exc)

    try:
        user_ids = await _users_with_active_memories()
    except Exception as exc:  # noqa: BLE001
        log.warning("maintenance_user_enum_failed error=%s", exc)
        user_ids = []

    for uid in user_ids:
        try:
            await maintain_user_memories(uid)
            summary["users"] += 1
        except Exception as exc:  # noqa: BLE001 — one user failing != abort round.
            log.warning("maintain_user_failed user=%s error=%s", uid, exc)

    return summary


def _seconds_until_maintenance_hour(now: datetime, hour: int) -> float:
    """Seconds from *now* to the next occurrence of ``hour:00`` (UTC).

    Pure + injectable so the schedule math is unit-testable without real sleeps.
    If today's target already passed, roll to tomorrow.
    """
    target = now.replace(hour=hour % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()


async def memory_maintenance_loop() -> None:
    """Long-lived scheduler: sleep until the maintenance hour, run a round, repeat.

    Each iteration recomputes the delay to the next ``memory_maintenance_hour``
    (so it self-corrects after a long round) and sleeps it, then runs one round
    under try/except. Cancellation (app shutdown) propagates cleanly.
    """
    log.info("memory_maintenance_loop_started")
    while True:
        try:
            delay = _seconds_until_maintenance_hour(
                _utcnow(), get_settings().memory_maintenance_hour
            )
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        try:
            summary = await run_maintenance_round()
            log.info("memory_maintenance_round_done summary=%s", summary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad round must not kill the loop.
            log.warning("memory_maintenance_round_failed error=%s", exc)


def start_memory_maintenance() -> asyncio.Task | None:
    """Start the nightly loop as a strongly-referenced task, or None when disabled.

    Called from ``app.py`` lifespan. Returns None (and starts nothing) when
    ``memory_maintenance_enabled`` is False — the cleanest off-switch: no task,
    no wakeups. Idempotent: a second call while one is already running is a no-op.
    """
    global _maintenance_task
    if not get_settings().memory_maintenance_enabled:
        log.info("memory_maintenance_disabled")
        return None
    if _maintenance_task is not None and not _maintenance_task.done():
        return _maintenance_task
    _maintenance_task = asyncio.create_task(memory_maintenance_loop())
    return _maintenance_task


async def stop_memory_maintenance() -> None:
    """Cancel + await the loop task on shutdown (clean teardown). No-op if unset."""
    global _maintenance_task
    task = _maintenance_task
    _maintenance_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown swallows.
        pass
