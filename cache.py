"""TTL cache for upstream API responses, persisted in SQLite.

The feed's on-demand expansions — a release's tracklist, its streaming links,
an artist's MusicBrainz links — are pure reads of slowly-changing upstream
data. Fetching each once and remembering the result (across restarts, not just
for the browser session) removes repeat API round-trips, and with them the
1-second MusicBrainz pacing that makes those expansions feel slow.

Entries live in the ``api_cache`` table under ``kind:id`` keys with an ISO
timestamp. ``cached()`` serves a fresh-enough entry or calls the fetch and
stores the result. Empty/falsy results are never stored: several fetchers
swallow upstream errors and return ``[]``, and remembering that as "nothing"
would hide real data until the entry expired.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Awaitable, Callable

import db

# Tracklists are effectively immutable; streaming links and an artist's
# external links change occasionally, so they get shorter TTLs.
TTL_TRACKS = 30 * 24 * 3600     # 30 days
TTL_STREAMING = 24 * 3600       # 1 day
TTL_MB_LINKS = 7 * 24 * 3600    # 7 days

# Concurrent requests for the same key (e.g. a startup backfill racing a UI
# expand) share one fetch instead of both hitting the API.
_locks: dict[str, asyncio.Lock] = {}


def _age_seconds(fetched_at: str) -> float | None:
    """Seconds since an entry was stored, or None when unparsable."""
    try:
        dt = datetime.fromisoformat(fetched_at)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _read(key: str, ttl: float) -> object | None:
    entry = db.cache_get(key)
    if entry is None:
        return None
    value, fetched_at = entry
    age = _age_seconds(fetched_at)
    if age is None or age >= ttl:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def store(key: str, payload) -> None:
    """Populate the cache directly (used by the scan/backfill warm-up path)."""
    if not payload:
        return
    try:
        db.cache_put(key, json.dumps(payload))
    except Exception:
        pass


async def cached(
    key: str,
    ttl: float,
    fetch: Callable[[], Awaitable],
):
    """Return a cached value, or await ``fetch()`` and cache its result.

    ``fetch`` exceptions propagate (and are not cached), so callers keep their
    existing upstream-error handling.
    """
    hit = _read(key, ttl)
    if hit is not None:
        return hit

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Another waiter may have filled the cache while we blocked.
        hit = _read(key, ttl)
        if hit is not None:
            return hit
        payload = await fetch()
        store(key, payload)
        return payload
