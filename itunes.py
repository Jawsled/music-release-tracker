from __future__ import annotations

import asyncio
import logging
import time

import httpx

BASE_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"
USER_AGENT = "MusicReleaseTracker/0.1.0 (https://github.com/placeholder)"

logger = logging.getLogger("music-release-tracker.itunes")

_last_request_time: float = 0.0
_lock = asyncio.Lock()
_client: httpx.AsyncClient | None = None

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
    """Space request starts >=1s apart.

    Only the timestamp check is serialized; network I/O happens outside the
    lock so concurrent requests overlap in flight instead of queueing behind
    each other (previously a retry sleep blocked ALL requests).
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

    Uses the Lookup API (exact match by ID) instead of Search API (text search),
    which is more reliable and returns all releases without the 200-result limit.

    Classification logic:
    - Checks collectionName for " - Single" or " - EP" suffix (iTunes encodes type here)
    - Falls back to collectionType from iTunes (often unreliable, defaults to "Album")
    - Final fallback: infers type from trackCount:
      - <= 4 tracks: Single (appends " - Single" to title)
      - 5-12 tracks: EP (appends " - EP" to title)
      - > 12 tracks: Album
    """
    # Use Lookup API with artist ID - exact match, no filtering needed
    data = await _rate_limited_get(
        LOOKUP_URL,
        params={
            "id": artist_id,
                        "entity": "album",
            "country": country,
        },
    )
    results_list = data.get("results", [])

    seen_collection_ids = set()
    out: list[dict] = []

    for r in results_list:
        # Filter to only releases where this artist is the primary artist
        # (Lookup API can return albums where artist is a contributor/featured)
        if r.get("artistId") != artist_id:
            continue
        collection_id = r.get("collectionId")
        if not collection_id or collection_id in seen_collection_ids:
            continue

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
            continue
        if release_type == "EP" and not include_eps:
            continue
        # Always include Singles regardless of flags

        seen_collection_ids.add(collection_id)

        # Get artwork URL (iTunes provides 100x100, upscale to 600x600)
        artwork_url = ""
        artwork_100 = r.get("artworkUrl100", "")
        if artwork_100:
            artwork_url = artwork_100.replace("100x100bb", "600x600bb").replace("100x100", "600x600")

        # Normalize date to YYYY-MM-DD
        raw_date = r.get("releaseDate", "")
        normalized_date = normalize_date_for_sort(raw_date)

        out.append(
            {
                "id": collection_id,
                "title": r.get("collectionName", "") + title_suffix,
                "type": release_type,
                "date": normalized_date,
                "artistId": r.get("artistId"),
                "artistName": r.get("artistName", ""),
                "url": r.get("collectionViewUrl"),
                "artwork_url": artwork_url,
            }
        )

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
