"""Unit tests for the async Redis wrapper degradation contract (v3-M2).

Pure-unit: no real Redis server. Covers the PRD §11 backward-compat guarantee
that Redis being absent (empty URL) or unreachable (op raises) degrades every
operation to a silent no-op instead of ever raising / 500ing the chat path.
"""
from __future__ import annotations

import pytest

from src.infra.redis_client import RedisClient


class _BoomClient:
    """Fake raw client whose every op raises — simulates an unreachable Redis."""

    async def lpush(self, *a, **k):
        raise ConnectionError("redis down")

    async def hget(self, *a, **k):
        raise ConnectionError("redis down")


class _CountingClient:
    """Fake raw client that records calls and returns canned values."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def lpush(self, key, *values):
        self.calls.append(("lpush", key, values))
        return len(values)

    async def ltrim(self, key, start, stop):
        self.calls.append(("ltrim", key, start, stop))
        return True

    async def expire(self, key, seconds):
        self.calls.append(("expire", key, seconds))
        return True


@pytest.mark.asyncio
async def test_empty_url_disables_and_noops():
    """Empty REDIS_URL → feature dormant: enabled False, ops return None."""
    rc = RedisClient("")
    assert rc.enabled is False
    # No raise, returns None (callers treat as "no data" / pure-PG path).
    assert await rc.lpush("k", "v") is None
    assert await rc.ltrim("k", 0, 9) is None
    assert await rc.hgetall("k") is None
    assert await rc.expire("k", 60) is None


@pytest.mark.asyncio
async def test_connection_failure_trips_broken_and_noops():
    """A raising op degrades to no-op AND trips the broken flag for later ops."""
    rc = RedisClient("redis://unreachable:6379/0", client=_BoomClient())
    assert rc.enabled is True  # configured, not yet broken
    # First op raises internally → caught → returns None, feature marked broken.
    assert await rc.lpush("k", "v") is None
    assert rc.enabled is False
    # Subsequent ops short-circuit (no further raises even on a broken client).
    assert await rc.hget("k", "f") is None


@pytest.mark.asyncio
async def test_injected_client_ops_pass_through():
    """A healthy injected client makes enabled True and forwards calls."""
    fake = _CountingClient()
    rc = RedisClient("redis://x", client=fake)
    assert rc.enabled is True
    await rc.lpush("mkey", "a", "b")
    await rc.ltrim("mkey", 0, 5)
    await rc.expire("mkey", 100)
    assert [c[0] for c in fake.calls] == ["lpush", "ltrim", "expire"]
    assert fake.calls[0] == ("lpush", "mkey", ("a", "b"))
    assert fake.calls[1] == ("ltrim", "mkey", 0, 5)
    assert fake.calls[2] == ("expire", "mkey", 100)


@pytest.mark.asyncio
async def test_get_redis_singleton_and_reset():
    """get_redis returns a process-wide singleton; reset_redis clears it."""
    from src.infra.redis_client import get_redis, reset_redis

    reset_redis()
    a = get_redis()
    b = get_redis()
    assert a is b
    reset_redis()
    c = get_redis()
    assert c is not a
    reset_redis()
