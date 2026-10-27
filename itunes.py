from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

BASE_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"
USER_AGENT = "MusicReleaseTracker/0.1.0 (https://github.com/placeholder)"

logger = logging.getLogger("music-release-tracker.itunes")

_last_request_time: float = 0.0
_lock = asyncio.Lock()
_client: httpx.AsyncClient | None = None

# Apple documents roughly 20 calls/minute for the free Search API and routes
# heavier use to the Enterprise Partner Feed. The old 1s pacing (60/min) was
# over the limit and is the likely source of the 503 cooldowns.
MIN_REQUEST_INTERVAL = 3.0

COOLDOWN_503 = 5.0       # seconds to wait before first retry on 503
COOLDOWN_503_RETRY = 10.0  # seconds to wait before second retry on 503
MAX_503_RETRIES = 2       # max retries on 503 before giving up

MAX_NET_RETRIES = 3       # max retries for timeouts / connection errors
NET_BACKOFF_BASE = 2.0    # base backoff; doubles each retry (2s, 4s, 8s)


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    return _client


async def _pace_request() -> None:
    """Space request starts >=3s apart (Apple's documented ~20/min).

    Only the timestamp check is serialized; network I/O happens outside the
    lock so concurrent requests overlap in flight instead of queueing behind
    each other (previously a retry sleep blocked ALL requests).
    """
    global _last_request_time
    while True:
        async with _lock:
            now = time.monotonic()
            elapsed = now - _last_request_time
            if elapsed >= MIN_REQUEST_INTERVAL:
                _last_request_time = now
                return
            delay = MIN_REQUEST_INTERVAL - elapsed
        await asyncio.sleep(delay)


async def _rate_limited_get(url: str, params: dict) -> dict:
    """Make a GET request with rate limiting (starts spaced >=3s apart).

    Retries automatically on:
      - 503 errors (cooldown waits)
      - timeouts (e.g. ReadTimeout) and connection errors (exponential backoff)

    Raises the underlying exception once all retries are exhausted.
    Every retry is logged so it shows up in the UI log.
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
            if e.response.status_code == 503 and attempt < MAX_503_RETRIES:
                cooldown = COOLDOWN_503_RETRY if attempt > 0 else COOLDOWN_503
                logger.warning(
                    f"iTunes rate limit (503), retrying "
                    f"{attempt + 1}/{MAX_503_RETRIES} in {cooldown:.0f}s",
                    extra={"detail": url},
                )
                await asyncio.sleep(cooldown)
                continue
            logger.error(f"HTTP error {e.response.status_code} on {url}, giving up",
                         extra={"detail": str(e)})
            raise
        except httpx.TimeoutException as e:
            last_err = e
            if attempt >= MAX_NET_RETRIES:
                logger.error(f"Timeout on {url} after {attempt + 1} attempts, giving up",
                             extra={"detail": str(e)})
                raise
            delay = NET_BACKOFF_BASE * (2 ** attempt)
            logger.warning(
                f"Timeout, retrying {attempt + 1}/{MAX_NET_RETRIES} in {delay:.0f}s",
                extra={"detail": url},
            )
            await asyncio.sleep(delay)
            continue
        except httpx.RequestError as e:
            last_err = e
            if attempt >= MAX_NET_RETRIES:
                logger.error(f"Network error on {url} after {attempt + 1} attempts, giving up",
                             extra={"detail": str(e)})
                raise
            delay = NET_BACKOFF_BASE * (2 ** attempt)
            logger.warning(
                f"Network error ({type(e).__name__}), retrying "
                f"{attempt + 1}/{MAX_NET_RETRIES} in {delay:.0f}s",
                extra={"detail": url},
            )
            await asyncio.sleep(delay)
            continue

    assert last_err is not None
    raise last_err


async def search_artist(query: str, country: str = "us") -> list[dict]:
    """Search iTunes for artists matching the query."""
    data = await _rate_limited_get(
        BASE_URL,
        params={"term": query, "entity": "musicArtist", "limit": 5, "country": country},
    )

    results = []
    for a in data.get("results", []):
        # Get artist image URL (iTunes provides 100x100, downscale to 50x50 for search results)
        artist_image = a.get("artistImageUrl", "")
        if artist_image:
            artist_image = artist_image.replace("w100h100", "w50h50").replace("100x100", "50x50")

        results.append(
            {
                "artistId": a.get("artistId"),
                "name": a.get("artistName", ""),
                "disambiguation": a.get("primaryGenreName", ""),
                "country": country,
                "score": a.get("score", 0),
                "artistImageUrl": artist_image,
            }
        )
    return results


# Credits before a "feat."/"ft." marker are primary/co-primary artists; what
# follows is a featured appearance. Apple's artistId only names the primary
# artist, so co-primary collaborations need a name check against this part.
_FEAT_SPLIT_RE = re.compile(
    r"\s*(?:\(|\[)?\s*\b(?:feat\.?|ft\.?|featuring)\b", re.I
)
# Split collaboration credits, but only on standalone "x"/"vs." tokens and
# spaced "/"/"+" separators, so names like "X Ambassadors", "Malcolm X",
# "HUNTR/X" or "AC/DC" survive intact.
_CREDIT_SPLIT_RE = re.compile(
    r"\s*,\s*|\s*&\s*|\s+\+\s+|\s+/\s+|\s+\b(?:and|with|vs\.?)\b\s+|\s+x\s+",
    re.I,
)
_MAX_PRIMARY_CREDITS = 3


def _normalize_credit_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for credit matching."""
    folded = (name or "").casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", folded)).strip()


def _is_co_primary_credit(artist_name: str, credit: str) -> bool:
    """Whether ``artist_name`` is a primary/co-primary credit in ``credit``.

    Apple's ``artistId`` is only the collection's primary artist, so a
    collaboration credited to someone else ("Matt Sassari, Jack Back &
    David Guetta") is returned with another artist's ID. Match the tracked
    name against the credits that appear *before* any "feat." marker, and
    only the first few of them, so featured-only appearances stay out.
    """
    wanted = _normalize_credit_name(artist_name)
    if not wanted:
        return False
    primary = _FEAT_SPLIT_RE.split(credit or "", maxsplit=1)[0]
    if _normalize_credit_name(primary) == wanted:
        return True
    credits = [c for c in _CREDIT_SPLIT_RE.split(primary) if c.strip()]
    return any(
        _normalize_credit_name(c) == wanted
        for c in credits[:_MAX_PRIMARY_CREDITS]
    )


async def _search_fallback(artist_name: str, country: str) -> list[dict]:
    """One Search API query for an artist whose lookup hit the 200-row cap.

    Apple's Lookup API returns at most 200 collections and ignores ``offset``,
    so releases beyond that window (and releases filed under a duplicate
    Apple artist entry) never appear. The Search API ranks by relevance and
    does include those. This runs only for cap-hit artists, keeping the extra
    request bounded.
    """
    data = await _rate_limited_get(
        BASE_URL,
        params={
            "term": artist_name,
            "entity": "album",
            "attribute": "artistTerm",
            "limit": 200,
            "country": country,
        },
    )
    return data.get("results", []) or []


def _release_from_result(
    r: dict,
    artist_id: int,
    artist_name: str,
    include_albums: bool,
    include_eps: bool,
) -> dict | None:
    """Convert one iTunes collection result, or None when it should be skipped.

    A collection is kept when the tracked artist is its primary artist *or*
    a co-primary credit. Featured-only appearances are dropped.
    """
    if r.get("artistId") != artist_id and not _is_co_primary_credit(
        artist_name, r.get("artistName", "")
    ):
        return None

    collection_id = r.get("collectionId")
    if not collection_id:
        return None

    collection_name = r.get("collectionName", "")
    collection_type = r.get("collectionType", "")
    track_count = r.get("trackCount", 0)

    # Determine release type with classification fallback
    release_type = ""
    title_suffix = ""

    # Priority 1: Check collectionName for " - Single" or " - EP" suffix
    # iTunes encodes the actual release type in the name (e.g. "Music - Single")
    if collection_name.endswith(" - Single"):
        release_type = "Single"
    elif collection_name.endswith(" - EP"):
        release_type = "EP"
    # Priority 2: Use collectionType from iTunes (often just says "Album")
    elif collection_type:
        release_type = collection_type
    # Priority 3: Infer from track count
    elif track_count <= 4:
        release_type = "Single"
        title_suffix = " - Single"
    elif track_count <= 12:
        release_type = "EP"
        title_suffix = " - EP"
    else:
        release_type = "Album"

    # Skip if not wanted type
    if release_type == "Album" and not include_albums:
        return None
    if release_type == "EP" and not include_eps:
        return None
    # Always include Singles regardless of flags

    # Get artwork URL (iTunes provides 100x100, upscale to 600x600)
    artwork_url = ""
    artwork_100 = r.get("artworkUrl100", "")
    if artwork_100:
        artwork_url = artwork_100.replace("100x100bb", "600x600bb").replace("100x100", "600x600")

    # Normalize date to YYYY-MM-DD
    raw_date = r.get("releaseDate", "")
    normalized_date = normalize_date_for_sort(raw_date)

    return {
        "id": collection_id,
        "title": collection_name + title_suffix,
        "type": release_type,
        "date": normalized_date,
        "artistId": r.get("artistId"),
        "artistName": r.get("artistName", ""),
        "url": r.get("collectionViewUrl"),
        "artwork_url": artwork_url,
    }


async def get_artist_releases(
    artist_id: int,
    country: str = "us",
    include_albums: bool = True,
    include_eps: bool = True,
    limit: int = 200,
    artist_name: str = "",
) -> list[dict]:
    """
    Fetch artist releases using the iTunes Lookup API by artist ID.

    Uses the Lookup API (exact match by ID) as the primary source. Apple caps
    a lookup at 200 collections and ignores ``offset``; when the response is
    full, one Search API query by artist name fills in releases beyond that
    window (and releases filed under a duplicate Apple artist entry).

    A collection is kept when the tracked artist is its primary artist or a
    co-primary credit, so collaborations are no longer dropped just because
    another artist owns Apple's ``artistId``. Featured-only appearances stay
    out.

    Classification logic:
    - Checks collectionName for " - Single" or " - EP" suffix (iTunes encodes type here)
    - Falls back to collectionType from iTunes (often unreliable, defaults to "Album")
    - Final fallback: infers type from trackCount:
      - <= 4 tracks: Single (appends " - Single" to title)
      - 5-12 tracks: EP (appends " - EP" to title)
      - > 12 tracks: Album
    """
    data = await _rate_limited_get(
        LOOKUP_URL,
        params={
            "id": artist_id,
            "entity": "album",
            "limit": limit,
            "country": country,
        },
    )
    results_list = list(data.get("results", []))

    # Cap detection: resultCount includes the artist entry itself, so a full
    # response carries one more row than the requested collection limit.
    collection_count = sum(1 for r in results_list if r.get("collectionId"))
    if artist_name.strip() and collection_count >= limit:
        try:
            extra = await _search_fallback(artist_name, country)
        except Exception as e:
            logger.warning(f"iTunes search fallback failed for {artist_name!r}: {e}")
        else:
            seen_ids = {
                r.get("collectionId") for r in results_list if r.get("collectionId")
            }
            added = 0
            for r in extra:
                collection_id = r.get("collectionId")
                if collection_id and collection_id not in seen_ids:
                    seen_ids.add(collection_id)
                    results_list.append(r)
                    added += 1
            if added:
                logger.info(
                    f"iTunes search fallback added {added} release(s) "
                    f"for {artist_name!r}"
                )

    seen_collection_ids = set()
    out: list[dict] = []

    for r in results_list:
        release = _release_from_result(
            r, artist_id, artist_name, include_albums, include_eps
        )
        if release is None:
            continue
        collection_id = release["id"]
        if collection_id in seen_collection_ids:
            continue
        seen_collection_ids.add(collection_id)
        out.append(release)

    return out


def normalize_date_for_sort(date_str: str) -> str:
    """
    iTunes typically returns timestamps like '2019-08-16T07:00:00Z'.
    We normalize to 'YYYY-MM-DD' with padding:
      '' -> '0000-00-00'
      '2024' -> '2024-00-00'
      '2024-06' -> '2024-06-00'
      '2024-06-10T...' -> '2024-06-10'
    """
    if not date_str:
        return "0000-00-00"

    # strip timestamp
    date_part = date_str.split("T", 1)[0]

    parts = date_part.split("-")
    while len(parts) < 3:
        parts.append("00")

    return "-".join(parts[:3])


_album_count_cache: dict[tuple[int, str], tuple[int, bool]] = {}


async def get_artist_album_count(artist_id: int, country: str = "us") -> tuple[int, bool]:
    """Count an artist's albums on iTunes/Apple Music.

    Single lookup request (entity=album); the response's resultCount covers
    the artist entry plus releases, capped at the request limit. Returns
    (albums, capped). Results are cached per session.
    """
    key = (int(artist_id), country or "us")
    if key in _album_count_cache:
        return _album_count_cache[key]

    data = await _rate_limited_get(
        LOOKUP_URL,
        params={"id": key[0], "entity": "album", "limit": 200, "country": key[1]},
    )
    results = data.get("results", []) or []
    # resultCount includes the artist entry itself; collections carry collectionId
    albums = sum(1 for r in results if r.get("collectionId"))
    capped = len(results) >= 200 and data.get("resultCount", 0) >= 200
    _album_count_cache[key] = (albums, capped)
    return albums, capped


async def get_release_tracks(collection_id: int | str, country: str = "us") -> list[dict]:
    """Fetch tracks for an iTunes/Apple Music release by collection ID.

    Single lookup request with entity=song: the response contains the
    collection followed by all of its tracks (wrapperType=track), so no
    fuzzy text search is needed.
    """
    collection_id = int(collection_id)

    data = await _rate_limited_get(
        "https://itunes.apple.com/lookup",
        params={"id": collection_id, "entity": "song", "limit": 200, "country": country},
    )

    results = data.get("results", [])
    if not results:
        return []

    # Sort by track number
    songs = [
        s for s in results
        if s.get("wrapperType") == "track" and s.get("collectionId") == collection_id
    ]
    songs.sort(key=lambda s: (s.get("trackNumber", 0), s.get("trackTimeMillis", 0)))

    seen_track_numbers = set()
    tracks = []

    for s in songs:
        num = str(s.get("trackNumber", ""))
        title = s.get("trackName", "")
        if not title:
            continue

        if num in seen_track_numbers:
            continue
        seen_track_numbers.add(num)

        tracks.append({
            "number": num,
            "title": title,
            "length": s.get("trackTimeMillis", 0),  # iTunes returns milliseconds
        })

    return tracks


async def main():
    artists = await search_artist("Radiohead", country="us")
    if not artists:
        return

    # take top hit; you can pick another via ranking/disambiguation if needed
    artist_id = artists[0]["artistId"]

    releases = await get_artist_releases(artist_id, country="us", include_albums=True, include_eps=True)
    releases.sort(key=lambda x: normalize_date_for_sort(x["date"]))

    for r in releases[:15]:
        print(r["type"], r["title"], r["date"], "->", normalize_date_for_sort(r["date"]))


if __name__ == "__main__":
    asyncio.run(main())
