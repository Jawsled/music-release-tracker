"""Disk-cached proxy for Cover Art Archive release-group covers.

Fetching covers straight from coverartarchive.org means an external request
per feed card, slow first paints, and no memory of the many release groups
that simply have no art (each of those is re-requested on every load). This
module fetches each cover at most once, stores the bytes under
``.cache/covers/``, and remembers "no art" results so they are never retried.

Only definitive answers are cached: a 200 is stored as ``<mbid>.img`` and a
404/410 as an empty ``<mbid>.miss`` marker. Timeouts and other transient
failures are deliberately *not* cached, so a flaky moment doesn't permanently
hide a cover.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx

CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "covers"

CAA_URL = "https://coverartarchive.org/release-group/{mbid}/front-250"
USER_AGENT = "MusicReleaseTracker/0.1.0 (https://github.com/placeholder)"

MAX_CONCURRENT_FETCHES = 6
REQUEST_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

_MBID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_client: httpx.AsyncClient | None = None
_sem = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
_locks: dict[str, asyncio.Lock] = {}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
    return _client


def _sniff_content_type(data: bytes) -> str:
    """Best-effort media type from magic bytes (CAA serves mostly JPEG)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via a temp file so a reader never sees a half-written cover."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _read_cached(mbid: str) -> tuple[bytes, str] | None:
    """Return a cached (bytes, content_type), or None when nothing is cached."""
    img_path = CACHE_DIR / f"{mbid}.img"
    if img_path.exists():
        data = img_path.read_bytes()
        if data:
            return data, _sniff_content_type(data)
    return None


async def _fetch(mbid: str) -> tuple[str, bytes | None, str | None]:
    """Fetch a cover. Returns (status, data, content_type).

    status is "hit" (data is the image bytes), "miss" (definitively no art)
    or "error" (transient — caller must not cache it).
    """
    async with _sem:
        try:
            resp = await _get_client().get(
                CAA_URL.format(mbid=mbid), follow_redirects=True
            )
        except Exception:
            return "error", None, None

    if resp.status_code == 200 and resp.content:
        return "hit", resp.content, _sniff_content_type(resp.content)
    if resp.status_code in (404, 410):
        return "miss", None, None
    return "error", None, None


async def get_cover(mbid: str) -> tuple[bytes, str] | None:
    """Return (image_bytes, content_type) for a release-group cover, or None.

    Cached on disk. Concurrent requests for the same release group share a
    single upstream fetch.
    """
    if not mbid or not _MBID_RE.match(mbid):
        return None

    mbid = mbid.lower()
    miss_path = CACHE_DIR / f"{mbid}.miss"

    cached = _read_cached(mbid)
    if cached is not None:
        return cached
    if miss_path.exists():
        return None

    lock = _locks.setdefault(mbid, asyncio.Lock())
    async with lock:
        # Another waiter may have populated the cache while we blocked.
        cached = _read_cached(mbid)
        if cached is not None:
            return cached
        if miss_path.exists():
            return None

        status, data, content_type = await _fetch(mbid)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if status == "hit" and data:
            _atomic_write(CACHE_DIR / f"{mbid}.img", data)
            return data, content_type or "image/jpeg"
        if status == "miss":
            _atomic_write(miss_path, b"")
        return None
