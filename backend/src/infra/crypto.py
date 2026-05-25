"""At-rest encryption for user-supplied API keys (v2-M1).

Fernet symmetric crypto with key deterministically derived from settings.jwt_secret
via SHA-256. No new env var, no KMS — reuses the secret that already protects JWTs.

Caveat: rotating jwt_secret invalidates encrypted blobs (they become unreadable).
For dev / single-instance prod this is acceptable; if you ever need to rotate,
write a one-shot decrypt-with-old-key + re-encrypt-with-new-key script.
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from src.settings import get_settings


@lru_cache
def _fernet() -> Fernet:
    secret = get_settings().jwt_secret.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt(plain: str) -> str:
    """Encrypt plaintext. Empty input -> empty output (so we don't store '' as a token)."""
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Decrypt a Fernet token. Empty input -> empty output.

    Raises ValueError if the token is malformed or was encrypted with a different key.
    """
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("decrypt failed: invalid token or wrong key") from e
