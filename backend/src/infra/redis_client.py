"""Async Redis client wrapper with graceful degradation (v3-M2 memory-optimization).

Single switch-point for "is short-term memory hot-storage available". Mirrors the
lazy-singleton pattern of ``infra/vector_store.py:get_store``.

Degradation contract (PRD §11 backward-compat — never 500 the chat path):
  * ``REDIS_URL`` empty  → ``enabled`` is False forever; every op is an instant
    no-op. Callers fall back to the pure-PG path (M1 behavior).
  * ``REDIS_URL`` set but unreachable → the first failing op trips ``_broken``,
    logs one warning, and all later ops no-op for the process lifetime. A short
    socket timeout keeps a down Redis from stalling the request; PG fallback
    still serves compression bookkeeping + the L4 summary.

Only the handful of primitives the short-term memory path needs are exposed.
Every op returns ``None`` on degrade so callers treat that as "no data".
"""
from __future__ import annotations

import logging
from typing import Any

from src.settings import get_settings

log = logging.getLogger(__name__)

# Fail fast when Redis is down so a chat turn isn't blocked on a dead socket.
_CONNECT_TIMEOUT = 2
_OP_TIMEOUT = 2


class RedisClient:
    """Thin async wrapper over ``redis.asyncio`` with per-op degrade-to-no-op."""

    def __init__(self, url: str, *, client: Any | None = None) -> None:
        self._url = url or ""
        self._raw = client            # pre-built client (tests inject fakeredis)
        self._broken = False          # tripped after a connection / op failure

    @property
    def enabled(self) -> bool:
        """True when Redis is configured (URL or injected client) and not broken."""
        return bool(self._url or self._raw is not None) and not self._broken

    async def _get_raw(self):
        if self._raw is not None:
            return self._raw
        if not self._url:
            return None
        # Lazy connect on first real use (import kept local so the dependency is
        # only required when REDIS_URL is actually configured).
        import redis.asyncio as aioredis

        self._raw = aioredis.from_url(
            self._url,
            decode_responses=True,
            socket_connect_timeout=_CONNECT_TIMEOUT,
            socket_timeout=_OP_TIMEOUT,
        )
        return self._raw

    async def _run(self, method: str, *args: Any, **kwargs: Any):
        if not self.enabled:
            return None
        try:
            raw = await self._get_raw()
            if raw is None:
                return None
            return await getattr(raw, method)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — any Redis error must degrade, not raise.
            if not self._broken:
                log.warning("redis_unavailable_degrading method=%s error=%s", method, exc)
            self._broken = True
            return None

    # ---- list ops (short-term message window) ----
    async def lpush(self, key: str, *values: str):
        return await self._run("lpush", key, *values)

    async def ltrim(self, key: str, start: int, stop: int):
        return await self._run("ltrim", key, start, stop)

    async def lrange(self, key: str, start: int, stop: int):
        return await self._run("lrange", key, start, stop)

    async def llen(self, key: str):
        return await self._run("llen", key)

    # ---- hash ops (session meta + user profile) ----
    async def hset(self, key: str, mapping: dict[str, str]):
        return await self._run("hset", key, mapping=mapping)

    async def hget(self, key: str, field: str):
        return await self._run("hget", key, field)

    async def hgetall(self, key: str):
        return await self._run("hgetall", key)

    # ---- key ops ----
    async def expire(self, key: str, seconds: int):
        return await self._run("expire", key, seconds)

    async def delete(self, *keys: str):
        return await self._run("delete", *keys)


# ---------------------------------------------------------------------------
# Lazy singleton (same shape as vector_store.get_store / reset_store)
# ---------------------------------------------------------------------------
_client: RedisClient | None = None


def get_redis() -> RedisClient:
    """Return the process-wide Redis client singleton (built from REDIS_URL)."""
    global _client
    if _client is None:
        _client = RedisClient(get_settings().redis_url)
    return _client


def reset_redis() -> None:
    """Test helper: clear the cached singleton so the next get_redis() rebuilds."""
    global _client
    _client = None


def set_redis(client: RedisClient) -> None:
    """Test seam: inject a RedisClient (e.g. wrapping fakeredis)."""
    global _client
    _client = client
