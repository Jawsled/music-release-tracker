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

MAX_NET_RETRIES = 3       # max retries for timeouts / connection errors
NET_BACKOFF_BASE = 2.0    # base backoff for network errors; doubles each retry (2s, 4s, 8s)


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    return _client


def _retry_after_seconds(headers) -> float | None:
    """Parse the Retry-After header (seconds form) if present."""
    raw = headers.get("Retry-After") if headers else None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (ValueError, TypeError):
        return None


async def _pace_request() -> None:
    """Space request starts >=1s apart (MusicBrainz rate policy).

    Only the timestamp check is serialized; network I/O happens outside the
    lock so concurrent requests overlap in flight instead of queueing behind
    each other (previously a 5-10s retry sleep blocked ALL requests).
    """
    global _last_request_time
    while True:
        async with _lock:
            now = time.monotonic()
            elapsed = now - _last_request_time
            if elapsed >= 1.0:
                _last_request_time = now
                return
            delay = 1.0 - elapsed
        await asyncio.sleep(delay)


async def _rate_limited_get(url: str, params: dict) -> dict:
    """Make a GET request with rate limiting (starts spaced >=1s apart).

    Retries automatically on:
      - 503 rate limiting (cooldown waits, honors Retry-After)
      - timeouts (e.g. ReadTimeout) and connection errors (exponential backoff)

    Raises httpx.HTTPStatusError / httpx.RequestError once all retries are
    exhausted. Every retry/skip decision is logged so it shows up in the UI log.
    """
    last_err: Exception | None = None

    for attempt in range(max(MAX_503_RETRIES, MAX_NET_RETRIES) + 1):
        await _pace_request()

        client = _get_client()
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

        except httpx.HTTPStatusError as e:
            last_err = e
            status = e.response.status_code
            if status == 503 and attempt < MAX_503_RETRIES:
                cooldown = COOLDOWN_503_RETRY if attempt > 0 else COOLDOWN_503
                retry_after = _retry_after_seconds(e.response.headers)
                if retry_after:
                    cooldown = max(cooldown, min(retry_after, 60.0))
                logger.warning(
                    f"MusicBrainz rate limit (503), retrying "
                    f"{attempt + 1}/{MAX_503_RETRIES} in {cooldown:.0f}s",
                    extra={"detail": url},
                )
                await asyncio.sleep(cooldown)
                continue
            logger.error(
                f"HTTP error {status} on {url}, giving up",
                extra={"detail": str(e)},
            )
            raise

        except httpx.TimeoutException as e:
            last_err = e
            if attempt >= MAX_NET_RETRIES:
                logger.error(
                    f"Read timeout on {url} after {attempt + 1} attempts, giving up",
                    extra={"detail": str(e)},
                )
                raise
            delay = NET_BACKOFF_BASE * (2 ** attempt)
            logger.warning(
                f"Read timeout, retrying {attempt + 1}/{MAX_NET_RETRIES} in {delay:.0f}s",
                extra={"detail": url},
            )
            await asyncio.sleep(delay)
            continue

        except httpx.RequestError as e:
            last_err = e
            if attempt >= MAX_NET_RETRIES:
                logger.error(
                    f"Network error on {url} after {attempt + 1} attempts, giving up",
                    extra={"detail": str(e)},
                )
                raise
            delay = NET_BACKOFF_BASE * (2 ** attempt)
            logger.warning(
                f"Network error ({type(e).__name__}), retrying "
                f"{attempt + 1}/{MAX_NET_RETRIES} in {delay:.0f}s",
                extra={"detail": str(e)},
            )
            await asyncio.sleep(delay)
            continue

        except Exception as e:
            last_err = e
            logger.error(
                f"Unexpected error on {url}",
                extra={"detail": f"{type(e).__name__}: {e}"},
            )
            raise

    assert last_err is not None
    raise last_err


def _extract_credits(artist_credit, main_mbid: str = "") -> list[dict]:
    """Extract credited artists other than the tracked artist from a
    MusicBrainz artist-credit list.

    Each entry looks like {"name": "Stack$", "joinphrase": ", ",
    "artist": {"id": ..., "name": "Stacks"}}. The credited display name
    ("name") may differ from the canonical artist name.

    Returns an ordered, deduplicated [{"name": str, "mbid": str}, ...]
    excluding the tracked artist (matched by MBID).
    """
    if not artist_credit or not isinstance(artist_credit, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for entry in artist_credit:
        if not isinstance(entry, dict):
            continue
        artist = entry.get("artist") or {}
        name = entry.get("name") or artist.get("name", "")
        mbid = artist.get("id", "")
        if not name:
            continue
        # The tracked artist themselves is not a "credit" on their own release
        if main_mbid and mbid == main_mbid:
            continue
        key = mbid or f"name:{name.lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "mbid": mbid})
    return out


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


async def get_release_tracks(rg_id: str, main_mbid: str = "") -> list[dict]:
    """Fetch tracks for a release group from MusicBrainz.

    Queries /release with the release-group filter to find an actual release,
    then fetches that release with track info included (plus per-track
    artist-credit, piggybacked on the same request).

    main_mbid: MBID of the tracked artist; their own credit is filtered out
    of each track's credits so only featured/guest artists are listed.
    """
    # Step 1 – find a release belonging to this release group.
    # limit=1: only the first release is ever used, and the pacer already
    # spaces requests >=1s apart, so no extra sleep is needed here.
    data = await _rate_limited_get(
        f"{BASE_URL}/release",
        params={"release-group": rg_id, "fmt": "json", "limit": 1},
    )

    releases = data.get("releases") or []
    if not releases:
        return []

    # Use the first release found in this group
    release_mbid = releases[0].get("id")
    if not release_mbid:
        return []

    # Step 2 – fetch tracks for that specific release (include=recordings provides track info)
    rel = await _rate_limited_get(
        f"{BASE_URL}/release/{release_mbid}",
        params={"inc": "recordings+artist-credits", "fmt": "json"},
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
                "credits": _extract_credits(
                    (track.get("recording") or {}).get("artist-credit")
                    or track.get("artist-credit"),
                    main_mbid,
                ),
            })

    return all_tracks




# ---------------------------------------------------------------------------
# Streaming / external URL relationships
# ---------------------------------------------------------------------------

# Keyword-based mapping of MusicBrainz URL-relationship types to friendly
# streaming-service names. Matching on substrings keeps this robust across
# MB's varied naming ("Spotify Track", "Spotify Artist", etc.).
# Order matters: more specific keywords first so a compound name resolves
# to the intended service.
STREAMING_SERVICE_KEYWORDS = [
    ("apple music", "Apple Music",  "apple_music"),
    ("spotify",     "Spotify",      "spotify"),
    ("amazon",      "Amazon Music", "amazon_music"),
    ("deezer",      "Deezer",       "deezer"),
    ("tidal",       "Tidal",        "tidal"),
    ("soundcloud",  "SoundCloud",   "soundcloud"),
    ("qobuz",       "Qobuz",        "qobuz"),
    ("bandcamp",    "Bandcamp",     "bandcamp"),
    ("youtube",     "YouTube",      "youtube"),
]

# Generic relationship types that indicate streaming but don't name the
# specific service. When we see one of these we must look at the URL domain
# to determine which service it belongs to.
GENERIC_STREAMING_TYPES = {"streaming", "free streaming", "streaming (free)"}

# URL domain -> service mapping, used when the relationship type is generic.
# Each entry: (domain_substring, display_name, css_key)
STREAMING_URL_PATTERNS = [
    ("open.spotify.com",   "Spotify",      "spotify"),
    ("spotify.com",        "Spotify",      "spotify"),
    ("spotify.link",       "Spotify",      "spotify"),
    ("music.apple.com",    "Apple Music",  "apple_music"),
    ("itunes.apple.com",   "Apple Music",  "apple_music"),
    ("music.amazon.com",   "Amazon Music", "amazon_music"),
    ("amazon.com",         "Amazon Music", "amazon_music"),
    ("deezer.com",         "Deezer",       "deezer"),
    ("tidal.com",          "Tidal",        "tidal"),
    ("soundcloud.com",     "SoundCloud",   "soundcloud"),
    ("qobuz.com",          "Qobuz",        "qobuz"),
    ("bandcamp.com",       "Bandcamp",     "bandcamp"),
    ("youtube.com",        "YouTube",      "youtube"),
    ("youtu.be",           "YouTube",      "youtube"),
]


def _classify_streaming_service(type_str: str, url: str = "") -> dict | None:
    """Return {"service": label, "key": css_key} if the URL-relation is a
    known streaming service, else None.

    First tries to match on the relationship type string (e.g. "Spotify Track").
    If the type is generic (e.g. "streaming", "free streaming"), falls back to
    matching on the URL domain (e.g. "open.spotify.com" -> Spotify).
    """
    t = (type_str or "").lower()

    # Direct keyword match on the relationship type
    for kw, label, key in STREAMING_SERVICE_KEYWORDS:
        if kw in t:
            return {"service": label, "key": key}

    # Generic streaming type -> classify by URL domain
    if t in GENERIC_STREAMING_TYPES and url:
        u = url.lower()
        for domain, label, key in STREAMING_URL_PATTERNS:
            if domain in u:
                return {"service": label, "key": key}

    return None


async def get_release_streaming_urls(rg_id: str) -> list[dict]:
    """Fetch external streaming URLs for a release group.

    A release group can contain MANY concrete releases (e.g. Midnights has 86),
    and MusicBrainz's /release endpoint only returns the first 25 by default.
    Streaming links are often attached to releases beyond that window, so we
    paginate with limit=100 and walk every release, collecting each one's URL
    relationships (inc=url-rels). Only known streaming-service relationships
    are kept, deduplicated by service key (one link per service).

    To prefer the "standard" edition (e.g. "Midnights" over "Midnights (Til Dawn edition)"),
    we first identify the most common release title across all releases in the group,
    then collect streaming URLs in two passes:
    1. Standard edition releases first (preferred links)
    2. Remaining releases as fallback for any missing services

    Returns [{"service": "Spotify", "key": "spotify", "url": "..."}, ...].
    """
    limit = 100
    offset = 0
    max_pages = 10  # safety cap: 1000 releases is far beyond any real group

    # Collect ALL releases with their streaming URLs
    all_releases: list[dict] = []
    for _ in range(max_pages):
        data = await _rate_limited_get(
            f"{BASE_URL}/release",
            params={
                "release-group": rg_id,
                "inc": "url-rels",
                "fmt": "json",
                "limit": limit,
                "offset": offset,
            },
        )

        releases = data.get("releases") or []
        if not releases:
            break

        all_releases.extend(releases)

        total = data.get("release-count", 0)
        offset += limit
        if offset >= total:
            break

    if not all_releases:
        return []

    # Identify the standard edition: most common release title
    title_counts: dict[str, int] = {}
    for release in all_releases:
        title = release.get("title", "")
        title_counts[title] = title_counts.get(title, 0) + 1

    # The standard edition title is the one that appears most frequently
    standard_title = max(title_counts, key=title_counts.get)

    # Separate releases into standard edition and others
    standard_releases = [r for r in all_releases if r.get("title") == standard_title]
    other_releases = [r for r in all_releases if r.get("title") != standard_title]

    # Collect streaming URLs, deduplicating by service key
    seen_keys: set[str] = set()
    out: list[dict] = []

    def process_releases(releases: list[dict]):
        for release in releases:
            for url_rel in release.get("relations") or []:
                if url_rel.get("target-type") != "url":
                    continue

                url_obj = url_rel.get("url") or {}
                value = url_obj.get("resource", "")
                if not value:
                    continue

                mapping = _classify_streaming_service(url_rel.get("type", ""), value)
                if not mapping:
                    continue

                key = mapping["key"]
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                out.append({**mapping, "url": value})

    # Pass 1: standard edition (preferred)
    process_releases(standard_releases)

    # Pass 2: other releases (fallback for missing services)
    process_releases(other_releases)

    return out


async def get_artist_releases(mbid: str) -> list[dict]:
    """Fetch all official release groups for an artist.

    Uses the /release endpoint with status=official and inc=release-groups,
    then deduplicates by release group ID. This filters out bootlegs and
    unofficial releases that the /release-group endpoint cannot distinguish.
    Also fetches the URL to the release-group page from MusicBrainz.

    artist-credits is included on the same request (no extra API calls) so
    credited/featured artists on each release can be shown in the UI.

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
                "inc": "release-groups+artist-credits",
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
                "credits": _extract_credits(release.get("artist-credit"), mbid),
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
