from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

MOBILE_API = "https://bandcamp.com/api/mobile/24"
# Bandcamp's bot challenge flags browser-like User-Agents coming from
# non-browser TLS fingerprints (Python/OpenSSL), returning a tiny JS challenge
# page instead of the artist page. A plain application UA is served the real
# HTML, and the mobile API accepts it too.
USER_AGENT = "MusicReleaseTracker/0.1.0"

logger = logging.getLogger("music-release-tracker.bandcamp")

_last_request_time: float = 0.0
_lock = asyncio.Lock()
_client: httpx.AsyncClient | None = None

MIN_REQUEST_INTERVAL = 1.0
COOLDOWN_503 = 5.0
COOLDOWN_503_RETRY = 10.0
MAX_503_RETRIES = 2
MAX_NET_RETRIES = 3
NET_BACKOFF_BASE = 2.0

PROFILE_TTL = 60 * 60  # seconds
BAND_DETAILS_TTL = 60 * 60

# artist.bandcamp.com (optionally with a path). Only Bandcamp-hosted artist
# subdomains are supported; custom domains cannot be resolved without search.
_URL_RE = re.compile(
    r"^(?:https?://)?([A-Za-z0-9-]{1,80})\.bandcamp\.com(?:[/?#]|$)", re.I
)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9-]{1,80}$")

# Subdomains that are Bandcamp itself, not an artist profile.
_RESERVED_SUBDOMAINS = {
    "www", "bandcamp", "daily", "api", "m", "store", "help", "get", "about",
    "community", "discover", "search", "login", "signup", "artists", "labels",
    "music", "merch", "fan", "fans", "collection", "wishlist", "settings",
    "terms", "privacy", "jobs", "press", "developers", "status", "blog",
    "video", "live", "radio", "shows", "tools", "embed", "embedded",
}

_BAND_ID_RE = re.compile(r'data-band-id="(\d+)"')
_CLIENT_ITEMS_RE = re.compile(r'data-client-items="([^"]*)"')
_ITEM_ANCHOR_RE = re.compile(
    r'<li data-item-id="([^"]+)"[\s\S]{0,600}?<a href="([^"]+)"'
)
_OG_TITLE_RE = re.compile(
    r'<meta\s+property="og:title"\s+content="([^"]*)"', re.I
)

_profile_cache: dict[str, tuple[float, dict]] = {}
_band_details_cache: dict[str, tuple[float, dict]] = {}
_page_cache: dict[str, tuple[float, dict]] = {}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
    return _client


async def _pace_request() -> None:
    """Space request starts >=1s apart."""
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


async def _request(url: str, params: dict | None = None) -> httpx.Response:
    """GET with pacing and retries for throttling/network errors."""
    last_err: Exception | None = None
    client = _get_client()

    for attempt in range(max(MAX_503_RETRIES, MAX_NET_RETRIES) + 1):
        await _pace_request()
        try:
            resp = await client.get(url, params=params)
            if resp.status_code in (429, 503) and attempt < MAX_503_RETRIES:
                cooldown = COOLDOWN_503_RETRY if attempt > 0 else COOLDOWN_503
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        cooldown = max(cooldown, min(float(retry_after), 60.0))
                    except (TypeError, ValueError):
                        pass
                logger.warning(
                    f"Bandcamp rate limit ({resp.status_code}), retrying "
                    f"{attempt + 1}/{MAX_503_RETRIES} in {cooldown:.0f}s"
                )
                await asyncio.sleep(cooldown)
                continue
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            last_err = e
            logger.error(f"HTTP error {e.response.status_code} on {url}, giving up")
            raise
        except (httpx.TimeoutException, httpx.RequestError) as e:
            last_err = e
            if attempt >= MAX_NET_RETRIES:
                logger.error(f"Network error on {url} after {attempt + 1} attempts")
                raise
            delay = NET_BACKOFF_BASE * (2 ** attempt)
            logger.warning(
                f"Network error ({type(e).__name__}), retrying "
                f"{attempt + 1}/{MAX_NET_RETRIES} in {delay:.0f}s"
            )
            await asyncio.sleep(delay)

    assert last_err is not None
    raise last_err


async def _get_json(url: str, params: dict | None = None) -> dict:
    resp = await _request(url, params=params)
    return resp.json()


async def _get_text(url: str) -> str:
    resp = await _request(url)
    return resp.text


# ---------------------------------------------------------------------------
# URL / profile resolution
# ---------------------------------------------------------------------------

def _extract_host(value: str) -> str | None:
    """Return the artist subdomain host from a URL or plain handle."""
    text = (value or "").strip()
    if not text:
        return None

    match = _URL_RE.match(text)
    if match:
        sub = match.group(1).lower()
    elif "bandcamp.com" in text.lower():
        # e.g. https://bandcamp.com/artist (not a profile URL)
        return None
    elif _HANDLE_RE.match(text):
        # Plain handle: probe name.bandcamp.com. Only used by the explicit
        # Bandcamp link flow, never by the combined name search.
        sub = text.lower()
    else:
        return None

    if sub in _RESERVED_SUBDOMAINS:
        return None
    return f"{sub}.bandcamp.com"


def _host_from_url(url: str) -> str:
    """Extract the host from a canonical Bandcamp URL."""
    match = re.match(r"^https?://([^/]+)", (url or "").strip(), re.I)
    return match.group(1).lower() if match else ""


def normalize_profile_url(value: str) -> str | None:
    """Return the artist host (``artist.bandcamp.com``) for a URL or handle."""
    return _extract_host(value)


def _artwork_url(art_id) -> str:
    """Album/track cover URL. Bandcamp's _16 size is ~700x700."""
    try:
        art_id = int(art_id)
    except (TypeError, ValueError):
        return ""
    if art_id <= 0:
        return ""
    return f"https://f4.bcbits.com/img/a{art_id}_16.jpg"


def _release_date(value) -> str:
    """Normalize Bandcamp's date forms to YYYY-MM-DD.

    ``band_details`` returns '13 Nov 2026 00:00:00 GMT'; ``tralbum_details``
    returns a Unix timestamp. Empty/unknown values become ''.
    """
    if isinstance(value, (int, float)) and value > 0:
        try:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return ""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(text)
        if dt:
            return dt.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass

    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return "-".join(match.groups())
    return ""


def _release_type(item_type: str, title: str) -> str:
    """Map Bandcamp's album/track item type to the app's release types.

    Bandcamp has no EP type. Track items are singles; album items are albums
    unless the title explicitly marks itself as an EP.
    """
    if (item_type or "").lower() == "track":
        return "Single"
    folded = (title or "").casefold().strip()
    if (
        folded.endswith(" - ep")
        or folded.endswith("(ep)")
        or folded.endswith("[ep]")
        or folded.endswith(" ep")
    ):
        return "EP"
    return "Album"


def _cached(cache: dict, key: str, ttl: float) -> dict | None:
    entry = cache.get(key)
    if not entry:
        return None
    fetched_at, value = entry
    if time.monotonic() - fetched_at > ttl:
        cache.pop(key, None)
        return None
    return value


def _store(cache: dict, key: str, value: dict) -> dict:
    cache[key] = (time.monotonic(), value)
    return value


async def _band_details(band_id: str) -> dict | None:
    """Fetch a band's profile + full discography from the mobile API."""
    band_id = str(band_id or "").strip()
    if not band_id:
        return None
    cached = _cached(_band_details_cache, band_id, BAND_DETAILS_TTL)
    if cached is not None:
        return cached
    try:
        data = await _get_json(
            f"{MOBILE_API}/band_details", params={"band_id": band_id}
        )
    except Exception as e:
        logger.warning(f"Bandcamp band_details failed for {band_id}: {e}")
        return None
    if not data or data.get("error"):
        return None
    return _store(_band_details_cache, band_id, data)


async def _fetch_artist_page(host: str) -> dict | None:
    """Fetch an artist page and extract the band id plus release URLs.

    Bandcamp sometimes serves a JS "Client Challenge" on the bare profile
    URL, so /music is tried first. The page's music grid exposes
    ``data-item-id`` -> ``/album/slug`` pairs, which lets most releases get a
    canonical URL without one API call per release.
    """
    host = (host or "").strip().lower()
    if not host:
        return None
    cached = _cached(_page_cache, host, PROFILE_TTL)
    if cached is not None:
        return cached

    for path in ("/music", "/"):
        try:
            html = await _get_text(f"https://{host}{path}")
        except Exception:
            continue
        if "Client Challenge" in html:
            continue
        band_match = _BAND_ID_RE.search(html)
        if not band_match:
            continue

        item_urls: dict[str, str] = {}

        def _absolute(href: str) -> str:
            """Make grid hrefs absolute (they are usually /album/... paths)."""
            href = (href or "").strip()
            if not href:
                return ""
            if href.startswith("//"):
                return f"https:{href}"
            if href.startswith("/"):
                return f"https://{host}{href}"
            if href.startswith("http://") or href.startswith("https://"):
                return href
            return f"https://{host}/{href}"

        # The grid's structured JSON carries clean page_url values.
        client_match = _CLIENT_ITEMS_RE.search(html)
        if client_match:
            raw = (
                client_match.group(1)
                .replace("&quot;", '"')
                .replace("&amp;", "&")
                .replace("&#39;", "'")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
            )
            try:
                import json
                for item in json.loads(raw):
                    item_id = item.get("id")
                    page_url = item.get("page_url")
                    item_type = item.get("type")
                    if item_id and page_url:
                        absolute = _absolute(page_url)
                        item_urls[str(item_id)] = absolute
                        if item_type:
                            item_urls[f"{item_type}-{item_id}"] = absolute
            except (ValueError, TypeError):
                pass

        # Fall back to (and top up from) the rendered grid anchors.
        for item_key, href in _ITEM_ANCHOR_RE.findall(html):
            href = _absolute(href)
            item_urls.setdefault(item_key, href)
            if "-" in item_key:
                item_urls.setdefault(item_key.split("-", 1)[1], href)

        name = ""
        og_match = _OG_TITLE_RE.search(html)
        if og_match:
            name = og_match.group(1).strip()
            if " | " in name:
                name = name.split(" | ", 1)[1].strip()

        return _store(_page_cache, host, {
            "bandcamp_url": host,
            "bandcamp_band_id": band_match.group(1),
            "name": name,
            "item_urls": item_urls,
        })

    return None


async def resolve_profile(value: str, band_id: str | None = None) -> dict | None:
    """Resolve a Bandcamp URL/handle to an artist profile.

    Returns {"bandcamp_url", "bandcamp_band_id", "name", "country",
    "artistImageUrl", "score", "source", "item_urls"} or None. With a known
    band id the profile is read from the mobile API directly; otherwise the
    artist page is fetched once to discover the id.
    """
    host = _extract_host(value)
    if host:
        cached = _cached(_profile_cache, host, PROFILE_TTL)
        if cached is not None and not band_id:
            return cached

    if not band_id and host:
        page = await _fetch_artist_page(host)
        if page:
            band_id = page.get("bandcamp_band_id")
            host = page.get("bandcamp_url") or host

    if not band_id:
        return None

    details = await _band_details(band_id)
    if not details:
        return None

    # Prefer the canonical host reported for this band id, so a stale or
    # wrong URL can never be paired with another band's numeric id.
    details_host = _host_from_url(details.get("bandcamp_url", ""))
    if details_host:
        host = details_host
    elif not host:
        return None

    profile = {
        "bandcamp_url": host,
        "bandcamp_band_id": str(band_id),
        "name": details.get("name") or host.split(".", 1)[0],
        "disambiguation": "",
        "country": details.get("location") or "",
        "score": 100,
        "source": "bandcamp",
        "artistImageUrl": "",
        "item_urls": {},
    }
    return _store(_profile_cache, host, profile)


async def search_artist(query: str) -> list[dict]:
    """Resolve a pasted Bandcamp URL or handle to a single profile suggestion.

    Bandcamp's site search sits behind a bot challenge, so there is no
    name-based search here. Plain handles are probed as ``handle.bandcamp.com``
    by the explicit link flow.
    """
    profile = await resolve_profile(query)
    return [profile] if profile else []


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

async def _tralbum_details(
    band_id: str, item_id: str, item_type: str
) -> dict | None:
    """Fetch one album/track's details (canonical URL + tracklist)."""
    tralbum_type = "t" if (item_type or "").lower() == "track" else "a"
    try:
        return await _get_json(
            f"{MOBILE_API}/tralbum_details",
            params={
                "band_id": band_id,
                "tralbum_id": item_id,
                "tralbum_type": tralbum_type,
            },
        )
    except Exception as e:
        logger.warning(
            f"Bandcamp tralbum_details failed for {item_id} ({item_type}): {e}"
        )
        return None


async def get_artist_releases(
    bandcamp_url: str,
    known_item_ids: set[str] | None = None,
    band_id: str | None = None,
) -> list[dict]:
    """Fetch a Bandcamp artist's full discography.

    ``known_item_ids`` are releases already stored for this artist. Their
    details are not re-fetched: the discography already carries title, date,
    type and art, and the stored row keeps its canonical URL. Only new items
    without a URL on the artist page cost one extra tralbum_details request.
    """
    profile = await resolve_profile(bandcamp_url, band_id=band_id)
    if not profile:
        return []
    bid = profile["bandcamp_band_id"]
    host = profile["bandcamp_url"]

    details = await _band_details(bid)
    if not details:
        return []

    discography = details.get("discography") or []
    known = {str(x) for x in (known_item_ids or set())}

    new_items = [
        item for item in discography
        if str(item.get("item_id") or "") not in known
    ]
    item_urls: dict[str, str] = {}
    if new_items:
        page = await _fetch_artist_page(host)
        if page:
            item_urls = page.get("item_urls") or {}

    releases: list[dict] = []
    for item in discography:
        item_id = str(item.get("item_id") or "").strip()
        if not item_id:
            continue
        item_type = (item.get("item_type") or "album").strip().lower()
        if item_type not in ("album", "track"):
            continue

        title = (item.get("title") or "").strip() or "Untitled"
        is_new = item_id not in known

        url = (
            item_urls.get(f"{item_type}-{item_id}")
            or item_urls.get(item_id)
            or ""
        )
        track_titles: list[str] = []
        if is_new and not url:
            tralbum = await _tralbum_details(bid, item_id, item_type)
            if tralbum:
                url = tralbum.get("bandcamp_url") or ""
                track_titles = [
                    (t.get("title") or "").strip()
                    for t in tralbum.get("tracks") or []
                    if (t.get("title") or "").strip()
                ]
        if not url and is_new:
            # No canonical URL found: link to the artist page rather than a
            # numeric id that Bandcamp does not resolve.
            url = f"https://{host}"

        releases.append({
            "mbid": item_id,
            "title": title,
            "type": _release_type(item_type, title),
            "date": _release_date(item.get("release_date")),
            "url": url,
            "artwork_url": _artwork_url(item.get("art_id")),
            "bandcamp_item_id": item_id,
            "bandcamp_item_type": item_type,
            "track_titles": track_titles,
            "credits": [],
        })

    return releases


async def get_release_tracks(
    item_id: str, item_type: str, band_id: str
) -> list[dict]:
    """Fetch an album/track's tracklist from the mobile API."""
    tralbum = await _tralbum_details(band_id, item_id, item_type)
    if not tralbum:
        return []

    tracks: list[dict] = []
    for index, track in enumerate(tralbum.get("tracks") or [], start=1):
        title = (track.get("title") or "").strip()
        if not title:
            continue
        try:
            duration_ms = int(float(track.get("duration") or 0) * 1000)
        except (TypeError, ValueError):
            duration_ms = 0
        tracks.append({
            "number": str(track.get("track_num") or index),
            "title": title,
            "length": duration_ms,
            "credits": [],
        })
    return tracks
