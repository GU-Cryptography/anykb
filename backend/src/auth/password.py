"""Password hashing using bcrypt.

bcrypt 5.x removed the convenience wrapper that older `passlib` patterns used;
we call bcrypt directly. Hashes include the salt + cost factor inline, so
verify() works without storing salt separately.
"""
from __future__ import annotations

import bcrypt

# Cost factor 12 = ~250ms hash time on a modern laptop. Tune up if hardware allows.
_ROUNDS = 12


def hash_password(plain: str) -> str:
    if not plain:
        raise ValueError("password cannot be empty")
    salt = bcrypt.gensalt(rounds=_ROUNDS)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
