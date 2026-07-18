"""Long-term memory management HTTP routes (v3-M4, PRD §8).

CRUD over the durable ``user_memories`` rows produced by the M3 extraction
pipeline (keyword / session-end). Authorization mirrors ``conversations/routes``:
Bearer JWT on every route, and each row double-scoped to its owner
(``user_id == uid AND id == mem_id``) — a forged or foreign memory id 404s
without leaking existence (agent-context-guidelines: per-user isolation).

The parallel vector index (``infra/memory_vector``) is kept in sync
best-effort: the PG row is the source of truth and is committed FIRST; the
vector delete / re-index runs after and swallows its own errors, so a vector
outage never fails a PG-authoritative edit or delete (error-handling.md /
vector-store-guidelines optional-infra contract). Any write that could change
the aggregated L1 profile also busts the profile hot cache.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.middleware import CurrentUser
from src.conversations.models import UserMemory
from src.infra.database import get_session

router = APIRouter(prefix="/api/memories", tags=["memories"])

# The stored memory_type enum (PRD §5.1). Used as a Literal so an unknown
# ?type= value is rejected with 422 by FastAPI's query validation.
MemoryType = Literal["profile", "preference", "fact", "task", "skill", "explicit"]


class PatchMemoryRequest(BaseModel):
    """PATCH body — at least one field must carry a value (enforced in handler).

    ``content`` min_length=1 rejects an empty string at validation; ``importance``
    is intentionally unbounded here and clamped to [0, 1] in the handler (task
    contract: clamp, don't reject out-of-range).
    """

    content: str | None = Field(default=None, min_length=1, max_length=4096)
    importance: float | None = Field(default=None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _load_owned_memory(
    session: AsyncSession, mem_id: str, user_id: str
) -> UserMemory:
    """Fetch a live memory owned by ``user_id``, else 404 (no existence leak).

    Double-scoped on purpose: ``session.get`` by pk, then an explicit owner
    check — a valid id belonging to another user is indistinguishable from a
    missing one. A soft-deleted row (v3-M5: the nightly maintenance job culls by
    stamping ``deleted_at``) is likewise treated as gone — it must not be
    PATCH-resurrected or double-deleted — so it 404s the same as a missing id.
    """
    mem = await session.get(UserMemory, mem_id)
    if mem is None or mem.user_id != user_id or mem.deleted_at is not None:
        raise HTTPException(status_code=404, detail="memory not found")
    return mem


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("")
async def list_memories(
    user: CurrentUser,
    memory_type: MemoryType | None = Query(default=None, alias="type"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List the current user's long-term memories, newest first (PRD §8).

    Always scoped to the authenticated user, excluding soft-deleted rows (v3-M5:
    the nightly maintenance job dedups / evicts by stamping ``deleted_at``).
    Optional ``?type=`` filters to one memory_type (invalid values → 422 via the
    Literal). Paginated with ``limit`` (1–200) / ``offset``; ``total`` is the
    unpaginated owner+filter count so the frontend can render page controls.

    ``stats`` (v3-M5) is a lightweight, filter-independent summary of the user's
    ACTIVE memories — ``{by_type: {type: count}, active_total}`` — from one extra
    aggregate query, so the page can render per-type count badges without a new
    endpoint.
    """
    active_only = UserMemory.deleted_at.is_(None)
    where = [UserMemory.user_id == user.id, active_only]
    if memory_type is not None:
        where.append(UserMemory.memory_type == memory_type)

    total = await session.scalar(
        select(func.count()).select_from(UserMemory).where(*where)
    )
    rows = (
        (
            await session.execute(
                select(UserMemory)
                .where(*where)
                .order_by(desc(UserMemory.created_at))
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    # One aggregate over ALL of the user's active memories (independent of the
    # ?type filter + pagination) → per-type counts for the stats bar.
    stats_rows = (
        await session.execute(
            select(UserMemory.memory_type, func.count())
            .where(UserMemory.user_id == user.id, active_only)
            .group_by(UserMemory.memory_type)
        )
    ).all()
    by_type = {mtype: int(cnt) for mtype, cnt in stats_rows}

    return {
        "total": total or 0,
        "limit": limit,
        "offset": offset,
        "memories": [m.to_public_dict() for m in rows],
        "stats": {"by_type": by_type, "active_total": sum(by_type.values())},
    }


@router.delete("/{mem_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    mem_id: str,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete one memory (owner-scoped) → PG row, then vector + profile cache.

    PG delete commits first (durable); the vector delete and cache bust run
    after and are best-effort so a vector-backend outage still deletes the row.
    """
    mem = await _load_owned_memory(session, mem_id, user.id)
    await session.delete(mem)
    await session.commit()

    from src.conversations.long_term_memory import invalidate_profile_cache
    from src.infra.memory_vector import delete_memory_vector

    await delete_memory_vector(mem_id)          # best-effort, never raises
    await invalidate_profile_cache(user.id)     # row gone → L1 aggregate stale


@router.patch("/{mem_id}")
async def patch_memory(
    mem_id: str,
    req: PatchMemoryRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Edit a memory's content and/or importance (owner-scoped, PRD §8).

    Requires at least one of ``content`` / ``importance`` (else 422). ``content``
    is stripped (empty → 422); ``importance`` is clamped to [0, 1]. On any actual
    change the PG row is committed, then the vector is re-indexed (re-embed +
    upsert, overwriting text/importance) and the profile cache is invalidated —
    both best-effort. Returns the updated memory DTO.
    """
    from src.conversations.long_term_memory import (
        _clamp01,
        invalidate_profile_cache,
        reindex_memory_vector,
    )

    if req.content is None and req.importance is None:
        raise HTTPException(status_code=422, detail="nothing to update")

    mem = await _load_owned_memory(session, mem_id, user.id)

    changed = False
    if req.content is not None:
        new_content = req.content.strip()
        if not new_content:
            raise HTTPException(status_code=422, detail="content must not be empty")
        if new_content != mem.content:
            mem.content = new_content
            changed = True
    if req.importance is not None:
        clamped = _clamp01(req.importance)
        if clamped != mem.importance:
            mem.importance = clamped
            changed = True

    await session.commit()
    await session.refresh(mem)

    if changed:
        # Re-index against the post-edit row (content + importance both current),
        # then bust the L1 cache — a content/importance edit can shift the
        # aggregated profile. Both are optional-infra best-effort.
        await reindex_memory_vector(
            mem_id, user.id, mem.memory_type, mem.content, mem.importance
        )
        await invalidate_profile_cache(user.id)

    return mem.to_public_dict()
