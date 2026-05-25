"""Simple in-memory IP rate limiter (per hour). Sufficient for v1."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

_buckets: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()


def check(ip: str, limit_per_hour: int) -> tuple[bool, int]:
    """Return (allowed, remaining)."""
    now = time.time()
    window = 3600
    with _lock:
        q = _buckets[ip]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit_per_hour:
            return False, 0
        q.append(now)
        return True, limit_per_hour - len(q)
