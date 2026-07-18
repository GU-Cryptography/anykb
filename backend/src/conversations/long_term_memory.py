"""Long-term memory + user profile (v3-M3 memory-optimization).

Implements PRD §5 (long-term memory) and §6 (user profile) on top of the M2
short-term memory scaffolding. Three extraction sources feed the durable
``user_memories`` table (+ a best-effort vector index, see
``infra/memory_vector.py``):

  * real-time keyword hits (``schedule_keyword_extraction``) — a user turn that
    trips ``keyword_extractor`` is formatted by the SESSION LLM into one memory.
  * session-end extraction (``extract_conversation_memories``) — the whole
    conversation is summarized by the SESSION LLM into a memory array. M3 ships
    the function + tests; M4 wires the ``POST /finalize`` HTTP trigger.
  * (M5, out of scope) timed dedup / decay.

Two read paths consume the memories:

  * L1 user profile (``get_user_profile``) — a pure-code aggregation of
    profile/preference/skill/fact rows, hot-cached in a Redis Hash (30d TTL),
    PG-aggregated on miss / when Redis is off.
  * L2 semantic recall (``retrieve_long_term_memories``) — ANN over the vector
    index, falling back to importance-ranked PG rows whenever embedding is
    unconfigured or the vector backend is unavailable.

Contracts honored here (see .trellis/spec/backend/):
  * LLM calls reuse the conversation's LLM (``resolve_session_llm`` +
    ``cfg.default_model``, never ``pick_model``, never a hardcoded vendor model)
    — agent-context-guidelines. Failures are logged and skipped.
  * Background jobs keep module-level task refs, own their DB session, pass plain
    values (ids/strings) not ORM instances, and wrap everything in try/except —
    background-tasks-guidelines.
  * The vector index and Redis are optional infra: any failure degrades (log +
    skip / PG fallback), the durable PG row is always written first, and chat is
    never broken — error-handling.md.
  * ``memory_auto_extract=False`` short-circuits the ENTIRE extraction chain
    (keyword + session-end) — PRD §11.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from src.conversations.short_term_memory import (
    resolve_session_llm,
    short_term_memory_enabled,
)
from src.infra.embedding import embed
from src.infra.memory_vector import upsert_memory_vector
from src.infra.redis_client import get_redis
from src.settings import get_settings

if TYPE_CHECKING:
    from src.settings_user import UserEmbeddingConfig, UserLLMConfig

log = logging.getLogger(__name__)

# Keep detached extraction tasks referenced so they aren't GC'd mid-flight
# (background-tasks-guidelines). Mirrors short_term_memory._bg_tasks.
_bg_tasks: set[asyncio.Task] = set()

# Profile Redis Hash (PRD §4.2): cross-session, 30-day TTL, invalidated on write.
_PROFILE_TTL = 2_592_000  # 30 days
# Cap list-valued profile fields so the L1 section stays within its 200-tok budget.
_MAX_PROFILE_LIST = 8

# Bound extraction prompts so one giant archived message can't produce an
# oversized (costly) call on the user's own key (mirrors short_term_memory caps).
_MAX_MSG_CHARS = 2000
_MAX_TRANSCRIPT_CHARS = 16000

# Memory types the profile aggregator reads (PRD §6.2 / task requirement F).
_PROFILE_SOURCE_TYPES = ("profile", "preference", "skill", "fact")
# Types the LLM may assign (keeps stored rows within a known enum).
_KEYWORD_TYPES = ("profile", "preference", "fact", "skill")
_CONVERSATION_TYPES = ("preference", "fact", "task", "skill")


# ---------------------------------------------------------------------------
# Keys + small helpers
# ---------------------------------------------------------------------------
def _profile_key(user_id: str) -> str:
    return f"user_memory:{user_id}:profile"


def _clamp01(x: Any, default: float = 0.5) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def empty_profile() -> dict[str, Any]:
    """The all-blank profile shape (single source of truth for the fields)."""
    return {
        "role": "",
        "preferences": [],
        "environment": "",
        "skills": [],
        "current_project": "",
    }


def profile_is_empty(profile: dict[str, Any] | None) -> bool:
    """True when there's nothing worth injecting as L1 (→ omit the layer)."""
    if not profile:
        return True
    return not (
        (profile.get("role") or "").strip()
        or (profile.get("environment") or "").strip()
        or (profile.get("current_project") or "").strip()
        or [p for p in (profile.get("preferences") or []) if str(p).strip()]
        or [s for s in (profile.get("skills") or []) if str(s).strip()]
    )


def _dedup_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        v = (it or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# Robust JSON parsing (LLM output may carry fences / prose — never raise)
# ---------------------------------------------------------------------------
def _strip_fence(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    return s


def _extract_json_span(text: str, open_ch: str, close_ch: str) -> str | None:
    """Slice from the first *open_ch* to the last *close_ch* (tolerates prose
    before/after the JSON). Returns None if the delimiters aren't both present.
    """
    s = _strip_fence(text)
    start = s.find(open_ch)
    end = s.rfind(close_ch)
    if start == -1 or end == -1 or end <= start:
        return None
    return s[start : end + 1]


def parse_memory_object(raw: str) -> dict[str, Any] | None:
    """Parse a single ``{type, content, importance}`` object out of LLM text.

    Fence/prose tolerant. Returns None (never raises) when the payload is absent
    or not a JSON object — the caller then simply stores nothing.
    """
    span = _extract_json_span(raw, "{", "}")
    if span is None:
        return None
    try:
        obj = json.loads(span)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def parse_memory_array(raw: str) -> list[dict[str, Any]]:
    """Parse a JSON array of memory objects out of LLM text.

    Fence/prose tolerant. Invalid / non-array payloads are dropped WHOLESALE
    (returns ``[]``, logged by the caller) rather than raising — a bad LLM
    response must never break the extraction job.
    """
    span = _extract_json_span(raw, "[", "]")
    if span is None:
        return []
    try:
        arr = json.loads(span)
    except (ValueError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    return [item for item in arr if isinstance(item, dict)]


def _coerce_memory(item: dict[str, Any], allowed_types: tuple[str, ...]) -> tuple[str, str, float] | None:
    """Validate one raw memory dict → (type, content, importance) or None."""
    mtype = str(item.get("type") or "").strip().lower()
    content = str(item.get("content") or "").strip()
    if not content or mtype not in allowed_types:
        return None
    importance = _clamp01(item.get("importance", 0.5))
    return mtype, content, importance


# ---------------------------------------------------------------------------
# Session-LLM text completion (agent-context-guidelines: session model, no
# pick_model, no hardcoded vendor; log+skip on failure). Mirrors the LLM call
# core of short_term_memory._compress_rounds.
# ---------------------------------------------------------------------------
async def _session_llm_complete(
    llm_cfg: "UserLLMConfig | None",
    system_msg: str,
    user_prompt: str,
    *,
    max_tokens: int = 512,
) -> str:
    from src.infra.llm import get_client, with_cache_control

    try:
        s = get_settings()
        client = get_client(llm_cfg)
        # Session default_model, deterministically — the user's own model (with
        # any per-conversation override already applied by resolve_session_llm).
        # Deliberately NOT pick_model: its long-input escalation would silently
        # bill the user's complex_model for an auxiliary extraction chore.
        model = llm_cfg.default_model if llm_cfg is not None else s.llm_default_model
        is_anthropic = (
            llm_cfg.provider == "anthropic"
            if llm_cfg is not None
            else s.llm_provider == "anthropic"
        )

        if not is_anthropic:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
            )
            return (resp.choices[0].message.content or "").strip()

        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=with_cache_control([{"type": "text", "text": system_msg}], llm_cfg),
            messages=[{"role": "user", "content": user_prompt}],
        )
        for block in resp.content or []:
            if getattr(block, "type", "") == "text":
                return (block.text or "").strip()
        return ""
    except Exception as exc:  # noqa: BLE001 — auxiliary LLM must never break chat.
        log.warning("memory_extract_llm_failed error=%s", exc)
        return ""


# ---------------------------------------------------------------------------
# cfg resolution from ids (background jobs pass ids, not ORM instances)
# ---------------------------------------------------------------------------
async def _resolve_user_embedding_by_id(user_id: str) -> "UserEmbeddingConfig | None":
    """Load the user's embedding cfg (BYOK) for memory vectorization / recall.

    Both write (vectorize) and read (recall) go through this SAME resolution so
    memory vectors and query vectors land in the same embedding space. Returns
    None (→ vector path skipped, PG fallback) on any failure or when the user
    hasn't configured embedding.
    """
    try:
        from src.auth.models import User
        from src.infra.database import get_session_factory
        from src.settings_user import resolve_user_embedding

        async with get_session_factory()() as session:
            user = await session.get(User, user_id)
            if user is None:
                return None
            return resolve_user_embedding(user)
    except Exception as exc:  # noqa: BLE001 — degrade to PG-only, never raise.
        log.warning("resolve_user_embedding_failed user=%s error=%s", user_id, exc)
        return None


async def _resolve_session_llm_by_ids(
    user_id: str, conv_id: str | None
) -> "UserLLMConfig | None":
    """Resolve the session LLM cfg from ids (for standalone extraction jobs)."""
    try:
        from src.auth.models import User
        from src.conversations.models import Conversation
        from src.infra.database import get_session_factory

        async with get_session_factory()() as session:
            user = await session.get(User, user_id)
            conv = await session.get(Conversation, conv_id) if conv_id else None
            return resolve_session_llm(user, conv)
    except Exception as exc:  # noqa: BLE001
        log.warning("resolve_session_llm_failed user=%s error=%s", user_id, exc)
        return None


# ---------------------------------------------------------------------------
# Persist one memory: durable PG row FIRST, then best-effort vector + cache bust
# ---------------------------------------------------------------------------
async def _persist_memory(
    user_id: str,
    memory_type: str,
    content: str,
    importance: float,
    source_conversation_id: str | None,
    embedding_cfg: "UserEmbeddingConfig | None",
) -> str | None:
    """Write a memory row, invalidate the profile cache, then (best-effort)
    vectorize. Returns the new memory id, or None on the (rare) PG write failure.

    Ordering matters: the PG row is the durable source of truth and is committed
    before any vector work, so an embedding/vector outage costs at most the ANN
    index entry (L2 still recalls the row via the PG importance fallback).
    """
    from src.conversations.models import UserMemory
    from src.infra.database import get_session_factory

    mem_id = str(uuid.uuid4())
    try:
        async with get_session_factory()() as session:
            session.add(
                UserMemory(
                    id=mem_id,
                    user_id=user_id,
                    memory_type=memory_type,
                    content=content,
                    importance=importance,
                    source_conversation_id=source_conversation_id,
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — a failed write is skipped, not raised.
        log.warning("memory_persist_failed user=%s error=%s", user_id, exc)
        return None

    # New memory may change the aggregated profile → drop the hot cache.
    await invalidate_profile_cache(user_id)

    # Best-effort vectorization (skipped entirely when embedding is unconfigured,
    # keeping write/read embedding spaces consistent — see _resolve_user_embedding_by_id).
    if embedding_cfg is not None:
        try:
            vec = await embed(content, cfg=embedding_cfg)
            await upsert_memory_vector(mem_id, vec, content, user_id, memory_type, importance)
        except Exception as exc:  # noqa: BLE001 — vector index is optional infra.
            log.warning("memory_vectorize_failed memory=%s error=%s", mem_id, exc)

    return mem_id


async def reindex_memory_vector(
    memory_id: str,
    user_id: str,
    memory_type: str,
    content: str,
    importance: float,
) -> None:
    """Re-index one memory's vector after an edit (M4 ``PATCH /api/memories/{id}``).

    Resolves the user's embedding cfg through the SAME seam as write/recall (so
    the edited vector stays in the read embedding space), re-embeds *content*,
    and upserts — overwriting the prior point's vector AND payload (new
    ``text`` + ``importance``). The vector store exposes only whole-point upsert
    (no payload-only patch), so an importance-only edit still re-embeds the
    unchanged content; that's the accepted cost of keeping the index consistent.

    Best-effort optional infra (error-handling.md / vector-store-guidelines): a
    no-op when embedding is unconfigured, and any failure is logged + swallowed —
    the durable PG row (already committed by the caller) stays the source of
    truth, and L2 still recalls it via the importance-ranked PG fallback.
    """
    try:
        embedding_cfg = await _resolve_user_embedding_by_id(user_id)
        if embedding_cfg is None:
            return
        vec = await embed(content, cfg=embedding_cfg)
        await upsert_memory_vector(
            memory_id, vec, content, user_id, memory_type, importance
        )
    except Exception as exc:  # noqa: BLE001 — vector index is optional infra.
        log.warning("memory_reindex_failed memory=%s error=%s", memory_id, exc)


# ---------------------------------------------------------------------------
# User profile (L1): Redis hot Hash → PG aggregate fallback (PRD §6)
# ---------------------------------------------------------------------------
def _encode_profile_hash(profile: dict[str, Any]) -> dict[str, str]:
    return {
        "role": profile.get("role") or "",
        "preferences": json.dumps(profile.get("preferences") or [], ensure_ascii=False),
        "environment": profile.get("environment") or "",
        "skills": json.dumps(profile.get("skills") or [], ensure_ascii=False),
        "current_project": profile.get("current_project") or "",
    }


def _decode_profile_hash(raw: dict[str, str]) -> dict[str, Any]:
    def _load_list(v: str) -> list[str]:
        try:
            parsed = json.loads(v) if v else []
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []

    return {
        "role": raw.get("role") or "",
        "preferences": _load_list(raw.get("preferences") or ""),
        "environment": raw.get("environment") or "",
        "skills": _load_list(raw.get("skills") or ""),
        "current_project": raw.get("current_project") or "",
    }


async def _aggregate_profile_pg(user_id: str) -> dict[str, Any]:
    """Rebuild the profile from durable memory rows — pure code, no LLM (§6.2).

    role/environment take the single highest-importance profile/fact row;
    preferences/skills accumulate their rows (importance-ranked, deduped, capped).
    current_project has no source type in M3 (task rows aren't read here) → "".
    """
    from src.conversations.models import UserMemory
    from src.infra.database import get_session_factory

    profile = empty_profile()
    try:
        async with get_session_factory()() as session:
            rows = (
                (
                    await session.execute(
                        select(UserMemory)
                        .where(
                            UserMemory.user_id == user_id,
                            UserMemory.memory_type.in_(_PROFILE_SOURCE_TYPES),
                        )
                        .order_by(
                            UserMemory.importance.desc(), UserMemory.created_at.desc()
                        )
                    )
                )
                .scalars()
                .all()
            )
    except Exception as exc:  # noqa: BLE001 — degrade to empty profile, never raise.
        log.warning("profile_aggregate_failed user=%s error=%s", user_id, exc)
        return profile

    prefs: list[str] = []
    skills: list[str] = []
    for m in rows:  # already importance DESC, created_at DESC
        c = (m.content or "").strip()
        if not c:
            continue
        if m.memory_type == "profile" and not profile["role"]:
            profile["role"] = c
        elif m.memory_type == "fact" and not profile["environment"]:
            profile["environment"] = c
        elif m.memory_type == "preference":
            prefs.append(c)
        elif m.memory_type == "skill":
            skills.append(c)

    profile["preferences"] = _dedup_keep_order(prefs)[:_MAX_PROFILE_LIST]
    profile["skills"] = _dedup_keep_order(skills)[:_MAX_PROFILE_LIST]
    return profile


async def get_user_profile(user_id: str) -> dict[str, Any]:
    """Return the user's L1 profile dict (never raises; empty on any failure).

    Redis Hash hot read (when REDIS_URL is set) → PG aggregation on miss, then
    write-back with a 30d TTL. Redis empty/off/broken degrades straight to the
    PG aggregate (durable), so the feature works either way.
    """
    if not user_id:
        return empty_profile()

    redis_on = short_term_memory_enabled()
    if redis_on:
        try:
            hot = await get_redis().hgetall(_profile_key(user_id))
        except Exception:  # noqa: BLE001 — hot read is best-effort.
            hot = None
        if hot:
            return _decode_profile_hash(hot)

    profile = await _aggregate_profile_pg(user_id)

    if redis_on:
        try:
            redis = get_redis()
            await redis.hset(_profile_key(user_id), _encode_profile_hash(profile))
            await redis.expire(_profile_key(user_id), _PROFILE_TTL)
        except Exception:  # noqa: BLE001 — cache write-back is best-effort.
            pass
    return profile


async def invalidate_profile_cache(user_id: str) -> None:
    """Drop the hot profile Hash so the next read rebuilds from PG (§6.2)."""
    if not short_term_memory_enabled():
        return
    try:
        await get_redis().delete(_profile_key(user_id))
    except Exception:  # noqa: BLE001 — invalidation is best-effort.
        pass


# ---------------------------------------------------------------------------
# Long-term recall (L2): vector ANN → PG importance fallback (PRD §5, §7)
# ---------------------------------------------------------------------------
def _memory_to_dto(m: Any) -> dict[str, Any]:
    return {
        "id": m.id,
        "memory_type": m.memory_type,
        "content": m.content or "",
        "importance": m.importance,
    }


async def _load_memories_by_ids(user_id: str, ids: list[str]) -> list[dict[str, Any]]:
    """Load owner-scoped memory rows for *ids*, preserving the given order."""
    from src.conversations.models import UserMemory
    from src.infra.database import get_session_factory

    if not ids:
        return []
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(UserMemory).where(
                        UserMemory.user_id == user_id, UserMemory.id.in_(ids)
                    )
                )
            )
            .scalars()
            .all()
        )
    by_id = {r.id: r for r in rows}
    return [_memory_to_dto(by_id[i]) for i in ids if i in by_id]


async def _pg_top_memories(user_id: str, limit: int) -> list[dict[str, Any]]:
    """Fallback recall: top rows by importance DESC, created_at DESC (§7)."""
    from src.conversations.models import UserMemory
    from src.infra.database import get_session_factory

    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(UserMemory)
                    .where(UserMemory.user_id == user_id)
                    .order_by(
                        UserMemory.importance.desc(), UserMemory.created_at.desc()
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [_memory_to_dto(m) for m in rows]


async def retrieve_long_term_memories(
    user_id: str, query: str, limit: int = 3
) -> list[dict[str, Any]]:
    """Return up to *limit* long-term memories for L2 injection (never raises).

    Primary path (when the user has BYOK embedding configured AND the vector
    backend supports it): embed *query* → ANN filtered by user_id → load rows.
    Fallback (no embedding / vector miss / any failure): importance-ranked PG
    rows. Empty result → the caller omits the L2 layer.
    """
    if not user_id:
        return []

    try:
        ids: list[str] = []
        if query and query.strip():
            embedding_cfg = await _resolve_user_embedding_by_id(user_id)
            if embedding_cfg is not None:
                try:
                    vec = await embed(query.strip(), cfg=embedding_cfg)
                    ids = await search_memory_vectors_ids(vec, user_id, limit)
                except Exception as exc:  # noqa: BLE001 — degrade to PG fallback.
                    log.warning("memory_recall_vector_failed user=%s error=%s", user_id, exc)
                    ids = []
        if ids:
            hit = await _load_memories_by_ids(user_id, ids)
            if hit:
                return hit
        return await _pg_top_memories(user_id, limit)
    except Exception as exc:  # noqa: BLE001 — L2 is best-effort, never break planning.
        log.warning("memory_recall_failed user=%s error=%s", user_id, exc)
        return []


async def search_memory_vectors_ids(
    vec: list[float], user_id: str, limit: int
) -> list[str]:
    """Indirection over the vector module so tests can patch one seam; keeps the
    optional-infra degradation entirely inside ``infra/memory_vector``."""
    from src.infra.memory_vector import search_memory_vectors

    return await search_memory_vectors(vec, user_id, limit)


# ---------------------------------------------------------------------------
# Real-time keyword extraction (PRD §5.4) — scheduled from routes.append_message
# ---------------------------------------------------------------------------
_KEYWORD_SYSTEM = (
    "你是用户信息抽取助手。只输出一个 JSON 对象，不要输出任何解释或多余文本。"
)


def _keyword_prompt(content: str, hints: list[str]) -> str:
    hint_str = "、".join(hints) if hints else "无"
    return (
        "从下面这条用户消息中提取一条值得长期记住的用户信息。"
        f"规则命中的类别提示：{hint_str}。\n"
        "输出严格的 JSON 对象，字段：\n"
        '  type: 取 "profile"|"preference"|"fact"|"skill" 之一\n'
        "  content: 简洁中文描述，≤50 字，只保留稳定的用户信息\n"
        "  importance: 0.0-1.0 的重要度（用户明确要求记住时取高值）\n"
        '如果没有值得长期记住的信息，输出 {"type":"","content":"","importance":0}。\n'
        "只输出 JSON。\n\n"
        f"用户消息：{content}\n\nJSON："
    )


def schedule_keyword_extraction(
    user_id: str,
    conv_id: str,
    role: str,
    content: str,
    llm_cfg: "UserLLMConfig | None",
) -> None:
    """Fire-and-forget real-time keyword extraction after a user message.

    Short-circuits (no task, no LLM) when: auto-extract is off, the turn isn't a
    user turn, the content is blank, or no storable keyword category matches —
    so the common case costs one regex pass on the request thread and nothing
    more. Only a genuine hit spawns the background LLM formatting job.
    """
    if not get_settings().memory_auto_extract:
        return
    if role != "user" or not content or not content.strip():
        return

    from src.conversations.keyword_extractor import storable_categories

    hits = storable_categories(content)
    if not hits:
        return

    task = asyncio.create_task(
        _keyword_extract(user_id, conv_id, content, hits, llm_cfg)
    )
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _keyword_extract(
    user_id: str,
    conv_id: str,
    content: str,
    hits: list[str],
    llm_cfg: "UserLLMConfig | None",
) -> None:
    try:
        snippet = content.strip()
        if len(snippet) > _MAX_MSG_CHARS:
            snippet = snippet[:_MAX_MSG_CHARS] + "…（截断）"
        raw = await _session_llm_complete(
            llm_cfg, _KEYWORD_SYSTEM, _keyword_prompt(snippet, hits), max_tokens=256
        )
        obj = parse_memory_object(raw)
        if obj is None:
            return
        coerced = _coerce_memory(obj, _KEYWORD_TYPES)
        if coerced is None:
            return
        mtype, mcontent, importance = coerced
        embedding_cfg = await _resolve_user_embedding_by_id(user_id)
        await _persist_memory(
            user_id, mtype, mcontent, importance, conv_id, embedding_cfg
        )
    except Exception as exc:  # noqa: BLE001 — background best-effort, never raise.
        log.warning("keyword_extract_failed conv=%s error=%s", conv_id, exc)


# ---------------------------------------------------------------------------
# Session-end extraction (PRD §5.5) — function + tests only in M3 (M4 wires HTTP)
# ---------------------------------------------------------------------------
_CONVERSATION_SYSTEM = "你是对话分析助手。只输出一个 JSON 数组，不要输出任何解释或多余文本。"

_CONVERSATION_PROMPT_HEAD = (
    "分析以下对话，提取值得长期记住的信息。输出 JSON 数组，每项字段：\n"
    '  type: 取 "preference"|"fact"|"task"|"skill" 之一\n'
    "  content: 简洁中文描述，≤50 字\n"
    "  importance: 0.0-1.0\n"
    "没有值得记住的信息时输出空数组 []。只输出 JSON。\n\n"
)


def _build_transcript(messages: list[Any]) -> str:
    lines: list[str] = []
    for m in messages:
        speaker = "User" if m.role == "user" else "Assistant"
        text = (m.content or "").strip()
        if not text:
            continue
        if len(text) > _MAX_MSG_CHARS:
            text = text[:_MAX_MSG_CHARS] + "…（截断）"
        lines.append(f"{speaker}: {text}")
    transcript = "\n".join(lines)
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:_MAX_TRANSCRIPT_CHARS] + "…（截断）"
    return transcript


async def extract_conversation_memories(
    conversation_id: str,
    user_id: str,
    llm_cfg: "UserLLMConfig | None" = None,
) -> int:
    """Extract long-term memories from a whole conversation (PRD §5.5).

    Reads all messages, asks the SESSION LLM for a memory array, robustly parses
    it (fences / prose tolerated; malformed JSON dropped wholesale + logged,
    never raised), and writes each valid item to PG + the vector index. Returns
    the number of memories stored (0 on any short-circuit / failure).

    Self-contained for background use: owns its sessions and resolves llm/
    embedding cfgs from ids when not supplied (so a future 24h-idle scan can call
    it with just ids). ``memory_auto_extract=False`` short-circuits to 0.
    """
    if not get_settings().memory_auto_extract:
        return 0

    from src.conversations.models import Message
    from src.infra.database import get_session_factory

    try:
        async with get_session_factory()() as session:
            rows = (
                (
                    await session.execute(
                        select(Message)
                        .where(Message.conversation_id == conversation_id)
                        .order_by(Message.created_at)
                    )
                )
                .scalars()
                .all()
            )
            # Snapshot to plain values so nothing ORM-bound escapes the session
            # (background-tasks-guidelines: pass values, not ORM instances).
            snapshot = [
                SimpleNamespace(role=r.role, content=r.content) for r in rows
            ]
    except Exception as exc:  # noqa: BLE001
        log.warning("conversation_read_failed conv=%s error=%s", conversation_id, exc)
        return 0

    transcript = _build_transcript(snapshot)
    if not transcript.strip():
        return 0

    if llm_cfg is None:
        llm_cfg = await _resolve_session_llm_by_ids(user_id, conversation_id)

    raw = await _session_llm_complete(
        llm_cfg,
        _CONVERSATION_SYSTEM,
        _CONVERSATION_PROMPT_HEAD + transcript + "\n\nJSON 数组：",
        max_tokens=1024,
    )
    items = parse_memory_array(raw)
    if not items:
        # Empty is legitimate (nothing worth remembering) OR malformed (already
        # logged inside parse). Either way, store nothing — never raise.
        return 0

    embedding_cfg = await _resolve_user_embedding_by_id(user_id)
    stored = 0
    for item in items:
        coerced = _coerce_memory(item, _CONVERSATION_TYPES)
        if coerced is None:
            continue
        mtype, mcontent, importance = coerced
        mem_id = await _persist_memory(
            user_id, mtype, mcontent, importance, conversation_id, embedding_cfg
        )
        if mem_id:
            stored += 1
    return stored
