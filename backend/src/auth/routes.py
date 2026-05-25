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


@router.delete("/me")
async def delete_me(
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Hard-delete the current user.

    FK CASCADE handles owned KBs (via kb_members.user_id), conversations, etc.
    Note: KB.user_id is a soft FK; we explicitly clear owned KBs here to keep
    them from becoming orphaned. (Same for conversations.)
    """
    from sqlalchemy import delete
    from src.kb.models import KB
    from src.conversations.models import Conversation

    u = await session.get(User, user.id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")

    # Explicit cleanup since user_id is a soft FK on these tables.
    await session.execute(delete(Conversation).where(Conversation.user_id == user.id))
    await session.execute(delete(KB).where(KB.user_id == user.id))
    await session.delete(u)
    await session.commit()
    return {"ok": True}
