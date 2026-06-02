"""Admin dashboard HTTP routes (06-01).

Every endpoint is guarded by `AdminUser` (require_admin → 403 for non-admins,
401 for unauthenticated, banned users already rejected by current_user).

Privacy boundary: this API returns metadata and counts only. It never exposes
conversation message bodies or KB chunk text — see PRD "内容可见性 = 仅元数据".

Self-protection invariants (enforced server-side, 400/409):
  - an admin cannot ban / demote / delete themselves;
  - the last active admin cannot be demoted / banned / deleted;
  - system KBs cannot be deleted.

Destructive writes reuse existing logic (auth.routes.purge_user,
kb.routes.purge_kb) and leave a structlog "admin_action" breadcrumb.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.middleware import AdminUser
from src.auth.models import User
from src.auth.password import hash_password
from src.auth.routes import purge_user
from src.conversations.models import Conversation, Message
from src.infra.database import get_session
from src.kb.models import KB, Document
from src.kb.routes import _email_map, purge_kb

log = structlog.get_logger()

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _count(session: AsyncSession, model, *where) -> int:
    q = select(func.count()).select_from(model)
    for clause in where:
        q = q.where(clause)
    return int((await session.execute(q)).scalar_one())


async def _active_admin_count(session: AsyncSession, *, exclude: str | None = None) -> int:
    """Number of admins that are still active, optionally excluding one id."""
    q = (
        select(func.count())
        .select_from(User)
        .where(User.is_admin.is_(True), User.is_active.is_(True))
    )
    if exclude is not None:
        q = q.where(User.id != exclude)
    return int((await session.execute(q)).scalar_one())


async def _user_counts(session: AsyncSession, user_id: str) -> tuple[int, int]:
    kb = await _count(session, KB, KB.user_id == user_id)
    conv = await _count(session, Conversation, Conversation.user_id == user_id)
    return kb, conv


def _admin_user_dict(u: User, kb_count: int, conversation_count: int) -> dict:
    """User row for the admin views — public fields + platform flags + counts.

    Metadata only: no conversation/message content is read or returned.
    """
    return {
        **u.to_public_dict(),  # id, email, display_name, created_at, is_admin, is_active
        "byok_configured": u.llm_provider is not None,
        "kb_count": kb_count,
        "conversation_count": conversation_count,
    }


def _admin_kb_dict(kb: KB, owner: dict | None) -> dict:
    return {
        "id": kb.id,
        "name": kb.name,
        "description": kb.description,
        "owner_id": kb.user_id,
        "owner_email": (owner or {}).get("email"),
        "is_system": bool(kb.is_system),
        "documents_count": len(kb.documents) if kb.documents is not None else 0,
        "chunks_count": kb.chunks_count,
        "member_count": len(kb.members) if kb.members is not None else 0,
        "created_at": kb.created_at.isoformat() if kb.created_at else None,
    }


# ---------------------------------------------------------------------------
# Stats — dashboard overview (read-only)
# ---------------------------------------------------------------------------
@router.get("/stats")
async def stats(admin: AdminUser, session: AsyncSession = Depends(get_session)) -> dict:  # noqa: ARG001
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    return {
        "users": {
            "total": await _count(session, User),
            "active": await _count(session, User, User.is_active.is_(True)),
            "banned": await _count(session, User, User.is_active.is_(False)),
            "admins": await _count(session, User, User.is_admin.is_(True)),
            "new_last_7d": await _count(session, User, User.created_at >= cutoff),
        },
        "kbs": {
            "total": await _count(session, KB),
            "system": await _count(session, KB, KB.is_system.is_(True)),
        },
        "documents": await _count(session, Document),
        "conversations": await _count(session, Conversation),
        "messages": await _count(session, Message),
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
@router.get("/users")
async def list_users(
    admin: AdminUser,  # noqa: ARG001
    session: AsyncSession = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    total = await _count(session, User)
    rows = (
        await session.execute(
            select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    ids = [u.id for u in rows]
    kb_counts: dict[str, int] = {}
    conv_counts: dict[str, int] = {}
    if ids:
        kb_counts = dict(
            (
                await session.execute(
                    select(KB.user_id, func.count())
                    .where(KB.user_id.in_(ids))
                    .group_by(KB.user_id)
                )
            ).all()
        )
        conv_counts = dict(
            (
                await session.execute(
                    select(Conversation.user_id, func.count())
                    .where(Conversation.user_id.in_(ids))
                    .group_by(Conversation.user_id)
                )
            ).all()
        )

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "users": [
            _admin_user_dict(u, kb_counts.get(u.id, 0), conv_counts.get(u.id, 0))
            for u in rows
        ],
    }


@router.get("/users/{user_id}")
async def get_user(
    user_id: str,
    admin: AdminUser,  # noqa: ARG001
    session: AsyncSession = Depends(get_session),
) -> dict:
    u = await session.get(User, user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    kb_count, conv_count = await _user_counts(session, user_id)
    return _admin_user_dict(u, kb_count, conv_count)


class UpdateUserRequest(BaseModel):
    is_active: bool | None = None
    is_admin: bool | None = None


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    admin: AdminUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    if req.is_active is None and req.is_admin is None:
        raise HTTPException(status_code=400, detail="nothing to update")

    u = await session.get(User, user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")

    # Self-protection: you can't ban or demote your own account.
    if u.id == admin.id and (req.is_active is False or req.is_admin is False):
        raise HTTPException(status_code=400, detail="cannot ban or demote yourself")

    # Last-admin protection: blocking the action if it would drop the active
    # admin count to zero. An admin is "lost" by demotion or by being banned.
    drops_an_active_admin = u.is_admin and u.is_active and (
        req.is_admin is False or req.is_active is False
    )
    if drops_an_active_admin and await _active_admin_count(session, exclude=u.id) == 0:
        raise HTTPException(status_code=409, detail="cannot remove the last active admin")

    changes: dict[str, bool] = {}
    if req.is_active is not None and req.is_active != u.is_active:
        u.is_active = req.is_active
        changes["is_active"] = req.is_active
    if req.is_admin is not None and req.is_admin != u.is_admin:
        u.is_admin = req.is_admin
        changes["is_admin"] = req.is_admin

    if changes:
        await session.commit()
        await session.refresh(u)
        log.info("admin_action", actor=admin.email, action="update_user", target=u.email, changes=changes)

    kb_count, conv_count = await _user_counts(session, user_id)
    return _admin_user_dict(u, kb_count, conv_count)


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    req: ResetPasswordRequest,
    admin: AdminUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    u = await session.get(User, user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    u.password_hash = hash_password(req.new_password)
    await session.commit()
    log.info("admin_action", actor=admin.email, action="reset_password", target=u.email)
    return {"ok": True}


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    admin: AdminUser,
    session: AsyncSession = Depends(get_session),
) -> None:
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="cannot delete yourself")

    u = await session.get(User, user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")

    if u.is_admin and u.is_active and await _active_admin_count(session, exclude=u.id) == 0:
        raise HTTPException(status_code=409, detail="cannot delete the last active admin")

    await purge_user(session, u)
    log.info("admin_action", actor=admin.email, action="delete_user", target=u.email)


# ---------------------------------------------------------------------------
# Knowledge bases (cross-user)
# ---------------------------------------------------------------------------
@router.get("/kbs")
async def list_kbs(
    admin: AdminUser,  # noqa: ARG001
    session: AsyncSession = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    total = await _count(session, KB)
    rows = (
        await session.execute(
            select(KB).order_by(KB.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    owners = await _email_map(session, [kb.user_id for kb in rows])
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "kbs": [_admin_kb_dict(kb, owners.get(kb.user_id)) for kb in rows],
    }


@router.delete("/kbs/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb(
    kb_id: str,
    admin: AdminUser,
    session: AsyncSession = Depends(get_session),
) -> None:
    kb = await session.get(KB, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="kb not found")
    if kb.is_system:
        raise HTTPException(status_code=400, detail="cannot delete a system KB")
    await purge_kb(session, kb)
    log.info("admin_action", actor=admin.email, action="delete_kb", target=kb_id)
