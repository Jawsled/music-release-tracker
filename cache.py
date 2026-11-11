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
from datetime import date, datetime, timezone
from typing import Awaitable, Callable

import db

# Tracklists are effectively immutable once an entry is complete, but
# MusicBrainz seeds pre-release and freshly-released albums with only the
# tracks announced so far and fills the rest in over the following days, so
# those entries must not be trusted for a month. Fresh-enough releases
# re-check daily; everything else keeps the long TTL. Streaming links and an
# artist's external links change occasionally, so they get shorter TTLs.
TTL_TRACKS = 30 * 24 * 3600     # 30 days
TTL_TRACKS_FRESH = 24 * 3600    # 1 day (recently/future released albums)
TTL_FRESH_DAYS = 30             # releases dated within ±this many days
TTL_STREAMING = 24 * 3600       # 1 day
TTL_MB_LINKS = 7 * 24 * 3600    # 7 days


def tracks_ttl(release_date: str) -> float:
    """Tracklist TTL for one release, based on its release date.

    Future dates and dates within :data:`TTL_FRESH_DAYS` count as fresh —
    that is the window where MusicBrainz entries are still being filled in.
    Unparsable/partial dates count as fresh too: a missing date is more
    likely a data gap than a decades-old release, and the cost of one extra
    daily re-check is negligible.
    """
    text = (release_date or "").strip()
    try:
        released = date.fromisoformat(text)
    except ValueError:
        return TTL_TRACKS_FRESH
    today = datetime.now(timezone.utc).date()
    delta = (released - today).days
    if -TTL_FRESH_DAYS <= delta <= TTL_FRESH_DAYS:
        return TTL_TRACKS_FRESH
    return TTL_TRACKS


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
