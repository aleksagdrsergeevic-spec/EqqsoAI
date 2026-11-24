"""Per-key request rate limiting.

A runaway agent loop can burn a free tier's daily allowance in a couple of
minutes, so each client key gets a ceiling on requests per minute. The counter
lives in memory, which is enough for a single instance — that is what the
gateway is. RATE_LIMIT_RPM=0 turns the ceiling off.
"""

import os
import time
from collections import deque

from .errors import GatewayError

WINDOW_SECONDS = 60.0
DEFAULT_LIMIT = 600

_hits = {}


def limit() -> int:
    raw = os.environ.get("RATE_LIMIT_RPM")
    if not raw:
        return DEFAULT_LIMIT
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_LIMIT


def hit(bucket: str, ceiling: int, message: str) -> None:
    """Count one event in a named bucket; raise when it goes over the ceiling."""
    if ceiling <= 0:
        return
    now = time.monotonic()
    hits = _hits.setdefault(bucket, deque())
    while hits and now - hits[0] > WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= ceiling:
        retry_after = max(1, int(WINDOW_SECONDS - (now - hits[0])) + 1)
        raise GatewayError(
            429,
            f"{message} Retry in {retry_after}s.",
            type="rate_limit_error",
            code="rate_limit_exceeded",
        )
    hits.append(now)


def check(key: str) -> None:
    ceiling = limit()
    if ceiling <= 0:
        return
    hit(key, ceiling, f"Rate limit reached: {ceiling} requests per minute per key.")
