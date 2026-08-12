from __future__ import annotations

import asyncio
import logging
import time

import httpx

BASE_URL = "https://musicbrainz.org/ws/2"
USER_AGENT = "MusicReleaseTracker/0.1.0 (https://github.com/placeholder)"

logger = logging.getLogger("music-release-tracker.musicbrainz")

_last_request_time: float = 0.0
_lock = asyncio.Lock()
_client: httpx.AsyncClient = None

COOLDOWN_503 = 5.0       # seconds to wait before first retry on 503
COOLDOWN_503_RETRY = 10.0  # seconds to wait before second retry on 503
MAX_503_RETRIES = 2       # max retries on 503 before giving up


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=20.0,
        )
    return _client


async def _rate_limited_get(url: str, params: dict) -> dict:
    """Make a GET request with rate limiting and serialization.
    
    On 503 errors, waits for a cooldown period and retries up to MAX_503_RETRIES times.
    Raises httpx.HTTPStatusError if all retries are exhausted.
    """
    global _last_request_time
    last_err = None
    
    for attempt in range(MAX_503_RETRIES + 1):
        async with _lock:
            now = time.monotonic()
            elapsed = now - _last_request_time
            if elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed)
            _last_request_time = time.monotonic()

            client = _get_client()
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 503:
                    cooldown = COOLDOWN_503_RETRY if attempt > 0 else COOLDOWN_503
                    logger.warning(f"503 rate limit on {url} (attempt {attempt + 1}), retrying in {cooldown}s")
                    if attempt < MAX_503_RETRIES:
                        await asyncio.sleep(cooldown)
                        continue
                    else:
                        raise httpx.HTTPStatusError(
                            f"503 Service Unavailable after {MAX_503_RETRIES} retries",
                            request=resp.request,
                            response=resp
                        )
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                last_err = e
                if e.response.status_code == 503 and attempt < MAX_503_RETRIES:
                    cooldown = COOLDOWN_503_RETRY if attempt > 0 else COOLDOWN_503
                    await asyncio.sleep(cooldown)
                    continue
                logger.error(f"HTTP error {e.response.status_code} on {url}: {e}")
                raise
            except httpx.TimeoutException as e:
                last_err = e
                logger.error(f"Timeout on {url}: {e}")
                raise
            except httpx.RequestError as e:
                last_err = e
                logger.error(f"Request error on {url}: {e}")
                raise
            except Exception as e:
                last_err = e
                logger.error(f"Unexpected error on {url}: {type(e).__name__}: {e}")
                raise
    
    raise last_err


async def search_artist(query: str) -> list[dict]:
    """Search MusicBrainz for artists matching the query."""
    data = await _rate_limited_get(
        f"{BASE_URL}/artist/",
        params={"query": query, "fmt": "json"},
    )

    results = []
    for artist in data.get("artists", []):
        results.append({
            "mbid": artist["id"],
            "name": artist.get("name", ""),
            "disambiguation": artist.get("disambiguation", ""),
            "type": artist.get("type", ""),
            "country": artist.get("country", ""),
            "score": artist.get("score", 0),
        })
    return results


async def get_release_tracks(rg_id: str) -> list[dict]:
    """Fetch tracks for a release group from MusicBrainz.

    Queries /release with the release-group filter to find an actual release,
    then fetches that release with track info included.
    """
    # Step 1 – find a release belonging to this release group
    data = await _rate_limited_get(
        f"{BASE_URL}/release",
        params={"release-group": rg_id, "fmt": "json"},
    )

    releases = data.get("releases") or []
    if not releases:
        return []

    # Use the first release found in this group
    release_mbid = releases[0].get("id")
    if not release_mbid:
        return []

    # Step 2 – fetch tracks for that specific release (include=recordings provides track info)
    await asyncio.sleep(1.0)  # Rate limit: MusicBrainz requires ~1s between requests
    rel = await _rate_limited_get(
        f"{BASE_URL}/release/{release_mbid}",
        params={"inc": "recordings", "fmt": "json"},
    )

    seen_track_numbers = set()
    all_tracks = []

    media = rel.get("media") or []
    for medium in media:
        tracks = medium.get("tracks") or []
        for track in tracks:
            num = str(track.get("position", ""))
            title = track.get("title", "")
            if not title:
                continue

            key = (num, title)
            if key in seen_track_numbers:
                continue
            seen_track_numbers.add(key)

            all_tracks.append({
                "number": num,
                "title": title,
                "length": track.get("length", 0),
            })

    return all_tracks


async def get_artist_releases(mbid: str) -> list[dict]:
    """Fetch all official release groups for an artist.

    Uses the /release endpoint with status=official and inc=release-groups,
    then deduplicates by release group ID. This filters out bootlegs and
    unofficial releases that the /release-group endpoint cannot distinguish.
    Also fetches the URL to the release-group page from MusicBrainz.

    Fetches all primary release types (Album, EP, Single, Broadcast, Other) to match
    everything on the artist's releases page. Recordings are excluded as they are
    individual tracks rather than commercial releases.
    """
    seen_rg_ids = set()
    all_releases = []
    offset = 0
    limit = 100

    while True:
        data = await _rate_limited_get(
            f"{BASE_URL}/release",
            params={
                "artist": mbid,
                "type": "album|ep|single|broadcast|other",
                "status": "official",
                "inc": "release-groups",
                "fmt": "json",
                "limit": limit,
                "offset": offset,
            },
        )

        for release in data.get("releases", []):
            rg = release.get("release-group", {})
            rg_id = rg.get("id", "")
            if not rg_id or rg_id in seen_rg_ids:
                continue
            seen_rg_ids.add(rg_id)

            primary_type = rg.get("primary-type", "")
            if primary_type not in ("Album", "EP", "Single", "Broadcast", "Other"):
                continue
            # Normalize Broadcast/Other into "Other" category for UI
            if primary_type not in ("Album", "EP", "Single"):
                primary_type = "Other"

            # Use the official release's date instead of release-group's first-release-date
            # to avoid bootleg/unofficial dates polluting the data
            release_date = release.get("date", "")
            if not release_date:
                release_date = rg.get("first-release-date", "")

            all_releases.append({
                "mbid": rg_id,
                "title": rg.get("title", ""),
                "type": primary_type,
                "date": release_date,
                "url": f"https://musicbrainz.org/release-group/{rg_id}",
                "artist_mbid": mbid,  # Store artist MBID for linking
            })

        total = data.get("release-count", 0)
        offset += limit
        if offset >= total:
            break

    return all_releases


def normalize_date_for_sort(date_str: str) -> str:
    """Pad incomplete dates for consistent sorting.
    '2024' -> '2024-00-00', '2024-06' -> '2024-06-00', '' -> '0000-00-00'
    """
    if not date_str:
        return "0000-00-00"
    parts = date_str.split("-")
    while len(parts) < 3:
        parts.append("00")
    return "-".join(parts)
