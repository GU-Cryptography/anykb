"""FastAPI auth dependencies.

Usage:
    @app.get("/some-protected-endpoint")
    async def handler(user: User = Depends(current_user)):
        ...

Resolution:
    1. Read `Authorization: Bearer <token>` header
    2. Decode JWT → user_id
    3. Look up User in DB
    4. Return User, or raise 401
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import User
from src.auth.tokens import TokenError, decode_token
from src.infra.database import get_session
from src.settings import get_settings


async def current_user(
    authorization: Annotated[str | None, Header()] = None,
    session: AsyncSession = Depends(get_session),
) -> User:
    s = get_settings()
    if not s.auth_enabled:
        # Demo / dev mode: return a stub user (or first real user) without checking.
        # Useful for offline testing; never enable in production.
        stub = await session.get(User, "00000000-0000-0000-0000-000000000000")
        if stub is None:
            stub = User(
                id="00000000-0000-0000-0000-000000000000",
                email="demo@local",
                password_hash="!",
                display_name="demo",
            )
            session.add(stub)
            await session.commit()
        return stub

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user_id = payload["sub"]
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user no longer exists",
        )
    return user


CurrentUser = Annotated[User, Depends(current_user)]
