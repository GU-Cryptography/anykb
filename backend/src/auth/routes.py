"""Auth HTTP routes: POST /register, POST /login, GET /me, PATCH /me,
POST /change-password, DELETE /me (v3-M5)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.middleware import CurrentUser
from src.auth.models import User
from src.auth.password import hash_password, verify_password
from src.auth.tokens import issue_token
from src.infra.database import get_session

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=64)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    token: str
    user: dict


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(req: RegisterRequest, session: AsyncSession = Depends(get_session)) -> AuthResponse:
    # Check for existing email
    existing = await session.execute(select(User).where(User.email == req.email.lower()))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="email already registered")

    user = User(
        id=str(uuid.uuid4()),
        email=req.email.lower(),
        password_hash=hash_password(req.password),
        display_name=req.display_name or req.email.split("@")[0],
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    token = issue_token(user.id, user.email)
    return AuthResponse(token=token, user=user.to_public_dict())


@router.post("/login", response_model=AuthResponse)
async def login(req: LoginRequest, session: AsyncSession = Depends(get_session)) -> AuthResponse:
    result = await session.execute(select(User).where(User.email == req.email.lower()))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(req.password, user.password_hash):
        # Same error for "no such user" vs "wrong password" to avoid enumeration.
        raise HTTPException(status_code=401, detail="invalid email or password")

    # 06-01 admin-dashboard: banned accounts can authenticate-by-password but
    # must not receive a token. Distinct 403 so the UI can show a clear message.
    if not user.is_active:
        raise HTTPException(status_code=403, detail="account disabled")

    token = issue_token(user.id, user.email)
    return AuthResponse(token=token, user=user.to_public_dict())


@router.get("/me")
async def me(user: CurrentUser) -> dict:
    return user.to_public_dict()


# ---------------------------------------------------------------------------
# v3-M5: profile editing
# ---------------------------------------------------------------------------
class UpdateProfileRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)


@router.patch("/me")
async def update_me(
    req: UpdateProfileRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    u = await session.get(User, user.id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    u.display_name = req.display_name.strip()
    await session.commit()
    await session.refresh(u)
    return u.to_public_dict()


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)


@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    u = await session.get(User, user.id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    if not verify_password(req.old_password, u.password_hash):
        raise HTTPException(status_code=400, detail="旧密码不正确")
    u.password_hash = hash_password(req.new_password)
    await session.commit()
    return {"ok": True}


async def purge_user(session: AsyncSession, user: User) -> None:
    """Hard-delete a user plus the KBs and conversations they own.

    KB.user_id / Conversation.user_id are soft FKs (no DB-level cascade), so we
    clear them explicitly. Shared by the self-delete route (DELETE /me) and the
    admin delete-user endpoint; callers handle authorization + invariants first.

    NOTE: per-KB vector collections are not dropped here (bulk path, mirrors the
    original self-delete behavior). Single-KB deletion via kb.routes.purge_kb is
    what drops vector collections.

    v3-M3: long-term memory rows (user_memories) are cleared here too, and the
    matching vectors are dropped from the shared user_memory_vectors collection
    as a best-effort chain (PRD §5.7). The vector delete runs after the PG commit
    and swallows its own errors, so a vector-backend outage never blocks the
    user purge — orphaned vectors (filtered by a now-nonexistent user_id) are
    unreachable and harmless.
    """
    from sqlalchemy import delete

    from src.conversations.models import Conversation, UserMemory
    from src.kb.models import KB

    await session.execute(delete(UserMemory).where(UserMemory.user_id == user.id))
    await session.execute(delete(Conversation).where(Conversation.user_id == user.id))
    await session.execute(delete(KB).where(KB.user_id == user.id))
    user_id = user.id
    await session.delete(user)
    await session.commit()

    # Best-effort vector chain (both backends via the shared abstraction; the
    # local SQLite store is skipped inside the helper). Never raises.
    from src.infra.memory_vector import delete_memory_vectors_by_user

    await delete_memory_vectors_by_user(user_id)


@router.delete("/me")
async def delete_me(
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Hard-delete the current user (plus the KBs / conversations they own)."""
    u = await session.get(User, user.id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")

    await purge_user(session, u)
    return {"ok": True}
