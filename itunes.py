from __future__ import annotations

import asyncio
import time

import httpx

BASE_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"
USER_AGENT = "MusicReleaseTracker/0.1.0 (https://github.com/placeholder)"

_last_request_time: float = 0.0
_lock = asyncio.Lock()
_client: httpx.AsyncClient | None = None

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
                    # Rate limit hit - wait and retry
                    cooldown = COOLDOWN_503_RETRY if attempt > 0 else COOLDOWN_503
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
                raise
    
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


async def get_release_tracks(collection_id: int | str, country: str = "us") -> list[dict]:
    """Fetch tracks for an iTunes/Apple Music release by collection ID.

    Uses the iTunes Lookup API to find the album, then fetches its tracks
    via a song search scoped to that collection.
    """
    collection_id = int(collection_id)

    # Step 1 – look up the collection to get its name
    lookup_data = await _rate_limited_get(
        "https://itunes.apple.com/lookup",
        params={"id": collection_id, "country": country},
    )

    results = lookup_data.get("results", [])
    if not results:
        return []

    collection = results[0]
    collection_name = collection.get("collectionName", "")
    artist_name = collection.get("artistName", "")

    if not collection_name or not artist_name:
        return []

    # Step 2 – search for songs in this collection
    songs_data = await _rate_limited_get(
        BASE_URL,
        params={
            "term": f"{artist_name} {collection_name}",
            "entity": "song",
            "limit": 200,
            "country": country,
        },
    )

    # Filter to only songs belonging to this collection
    songs = [
        s for s in songs_data.get("results", [])
        if s.get("collectionId") == collection_id
    ]

    # Sort by track number
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
