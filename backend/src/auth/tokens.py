"""JWT issue / verify.

Token payload: {"sub": user_id, "email": email, "exp": <unix>, "iat": <unix>}
Algorithm: HS256 (symmetric, secret in env). For multi-service or production,
swap to RS256 with a key pair.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from src.settings import get_settings


class TokenError(Exception):
    """Raised on invalid / expired / malformed tokens."""


def issue_token(user_id: str, email: str) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=s.jwt_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate. Raises TokenError on any issue."""
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except jwt.ExpiredSignatureError as e:
        raise TokenError("token expired") from e
    except jwt.InvalidTokenError as e:
        raise TokenError(f"invalid token: {e}") from e
    if not payload.get("sub"):
        raise TokenError("token missing sub")
    return payload
