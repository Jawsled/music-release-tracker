from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

API_URL = "https://api-v2.soundcloud.com"
MOBILE_DISCOVER_URL = "https://m.soundcloud.com/discover"
DESKTOP_DISCOVER_URL = "https://soundcloud.com/discover"
USER_AGENT = "MusicReleaseTracker/0.1.0 (https://github.com/placeholder)"

logger = logging.getLogger("music-release-tracker.soundcloud")

_last_request_time: float = 0.0
_lock = asyncio.Lock()
_client: httpx.AsyncClient | None = None

COOLDOWN_503 = 5.0
COOLDOWN_503_RETRY = 10.0
MAX_503_RETRIES = 2

MAX_NET_RETRIES = 3
NET_BACKOFF_BASE = 2.0

CACHE_TTL_MS = 60 * 60 * 1000  # 1 hour
PAGE_LIMIT = 30
MAX_PAGES = 4

URL_RE = re.compile(
    r"^https?://(?:www\.|m\.)?soundcloud\.com/[A-Za-z0-9._-]{1,80}(?:/|$|\?)", re.I
)
SHORT_URL_RE = re.compile(r"^https?://on\.soundcloud\.com/[A-Za-z0-9._-]+")
PERMALINK_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
NUMERIC_ID_RE = re.compile(r"^\d{1,20}$")


class _Credentials:
    """Manages SoundCloud client_id and app_version bootstrapping."""

    def __init__(self):
        self.client_id: str = ""
        self.app_version: str = ""
        self.fetched_at: float = 0.0
        self._promise: asyncio.Task | None = None

    @property
    def valid(self) -> bool:
        return (
            self.client_id
            and self.app_version
            and (time.time() * 1000 - self.fetched_at) < CACHE_TTL_MS
        )

    async def get(self, client: httpx.AsyncClient) -> tuple[str, str]:
        if self.valid:
            return self.client_id, self.app_version
        if self._promise:
            cid, ver = await self._promise
            return cid, ver
        self._promise = asyncio.create_task(self._bootstrap(client))
        try:
            return await self._promise
        finally:
            self._promise = None

    async def _bootstrap(self, client: httpx.AsyncClient) -> tuple[str, str]:
        # Primary: mobile discover page (same as Grayjay plugin)
        try:
            resp = await client.get(MOBILE_DISCOVER_URL, follow_redirects=True)
            if resp.status_code == 200:
                html = resp.text
                cid_match = re.search(r'"clientId":"([a-zA-Z0-9-_]+)"', html)
                ver_match = re.search(r'"buildVersion":"([0-9]+)"', html)
                if cid_match and ver_match:
                    self.client_id = cid_match.group(1)
                    self.app_version = ver_match.group(1)
                    self.fetched_at = time.time() * 1000
                    logger.info("Bootstrapped SC credentials from mobile discover page")
                    return self.client_id, self.app_version
        except Exception as e:
            logger.warning(f"Mobile discover page failed: {e}")

        # Fallback: desktop discover page
        resp = await client.get(DESKTOP_DISCOVER_URL, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text

        ver_match = re.search(r'window\.__sc_version\s*=\s*"(\d+)"', html)
        if not ver_match:
            raise RuntimeError("Could not extract SoundCloud app_version from discover page")
        self.app_version = ver_match.group(1)

        bundle_urls = re.findall(
            r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html
        )
        if not bundle_urls:
            raise RuntimeError("Could not find any SoundCloud JS bundles on discover page")

        cid_re = re.compile(r'client_id["\']?\s*[:=]\s*["\']([a-zA-Z0-9_-]+)["\']')
        for url in bundle_urls[-5:]:
            try:
                js_resp = await client.get(url, follow_redirects=True)
                if js_resp.status_code == 200:
                    m = cid_re.search(js_resp.text)
                    if m:
                        self.client_id = m.group(1)
                        break
            except Exception:
                continue

        if not self.client_id:
            raise RuntimeError("Could not extract SoundCloud client_id from JS bundles")

        self.fetched_at = time.time() * 1000
        logger.info("Bootstrapped SC credentials from desktop discover page")
        return self.client_id, self.app_version

    def invalidate(self):
        self.client_id = ""
        self.app_version = ""
        self.fetched_at = 0.0


_creds = _Credentials()


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    return _client


def _with_creds(url: str, client_id: str, app_version: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}client_id={client_id}&app_version={app_version}&app_locale=en&linked_partitioning=1"


def _upgrade_artwork(url: str | None) -> str | None:
    if not url:
        return None
    return re.sub(
        r"-(?:large|small|mini|badge|t\d+x\d+|crop)(?=\.[a-z]+$)",
        "-t500x500",
        url,
        flags=re.I,
    )


def _normalize_handle(raw: str) -> str:
    return raw.strip().lstrip("@").lower()


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


async def _rate_limited_get(url: str, params: dict | None = None) -> dict:
    """Make a GET request with rate limiting (starts spaced >=1s apart)."""
    last_err: Exception | None = None
    client = _get_client()

    for attempt in range(max(MAX_503_RETRIES, MAX_NET_RETRIES) + 1):
        await _pace_request()

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

        except httpx.HTTPStatusError as e:
            last_err = e
            status = e.response.status_code
            if status == 401:
                # Stale credentials - invalidate and raise so caller can retry
                _creds.invalidate()
                raise
            if status == 503 and attempt < MAX_503_RETRIES:
                cooldown = COOLDOWN_503_RETRY if attempt > 0 else COOLDOWN_503
                retry_after_raw = e.response.headers.get("Retry-After")
                if retry_after_raw:
                    try:
                        cooldown = max(cooldown, min(float(retry_after_raw), 60.0))
                    except (ValueError, TypeError):
                        pass
                logger.warning(
                    f"SoundCloud rate limit (503), retrying "
                    f"{attempt + 1}/{MAX_503_RETRIES} in {cooldown:.0f}s"
                )
                await asyncio.sleep(cooldown)
                continue
            logger.error(f"HTTP error {status} on {url}, giving up")
            raise

        except httpx.TimeoutException as e:
            last_err = e
            if attempt >= MAX_NET_RETRIES:
                logger.error(f"Timeout on {url} after {attempt + 1} attempts")
                raise
            delay = NET_BACKOFF_BASE * (2 ** attempt)
            logger.warning(f"Timeout, retrying {attempt + 1}/{MAX_NET_RETRIES} in {delay:.0f}s")
            await asyncio.sleep(delay)
            continue

        except httpx.RequestError as e:
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
            continue

    assert last_err is not None
    raise last_err


async def _sc_get_json(url: str) -> dict:
    """GET with automatic credential bootstrapping and 401 retry."""
    client = _get_client()
    client_id, app_version = await _creds.get(client)
    cred_url = _with_creds(url, client_id, app_version)
    try:
        return await _rate_limited_get(cred_url)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            # Stale creds - bootstrap fresh and retry once
            client_id, app_version = await _creds.get(client)
            cred_url = _with_creds(url, client_id, app_version)
            return await _rate_limited_get(cred_url)
        raise


async def _fetch_text(url: str) -> str:
    client = _get_client()
    resp = await client.get(url, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def resolve_permalink(handle_or_url: str) -> str | None:
    """Resolve a SoundCloud URL or handle to a permalink (slug).

    Accepts:
      - Full URL: https://soundcloud.com/slayyyter
      - Short URL: https://on.soundcloud.com/...
      - Plain handle: slayyyter
    Returns the permalink slug (e.g. "slayyyter") or None.
    """
    text = handle_or_url.strip()

    # Short URL: follow redirect to get real URL
    if SHORT_URL_RE.match(text):
        try:
            resolved = await _fetch_text(text)
            if URL_RE.match(resolved):
                text = resolved
            else:
                return None
        except Exception:
            return None

    # Full URL
    url_match = URL_RE.match(text)
    if url_match:
        from urllib.parse import urlparse
        parsed = urlparse(text)
        path_segs = [s for s in parsed.path.split("/") if s]
        if not path_segs:
            return None
        first = path_segs[0].lower()
        if first in ("you", "discover", "stream", "search", "people", "stations"):
            return None
        if PERMALINK_RE.match(first):
            return first
        return None

    # Plain handle
    cleaned = _normalize_handle(text)
    if PERMALINK_RE.match(cleaned):
        return cleaned

    return None


async def search_artist(query: str) -> list[dict]:
    """Search for a SoundCloud artist by URL, permalink, or display name.

    SoundCloud has no text-search API like MB/iTunes. URLs and permalinks
    resolve to the exact profile first, then probe sibling slug variants
    so near-miss spellings surface too (six-impala -> also siximpala).
    A display name with spaces ("six impala") probes slug variants
    (siximpala, six-impala, six_impala) and returns each existing profile
    as a suggestion.
    """
    permalink = await resolve_permalink(query)
    if not permalink:
        if query and re.search(r"\s", query):
            return await suggest_by_name(query)
        return []

    exact = await _resolve_exact(permalink)
    results = [exact] if exact else []
    seen = {permalink}
    # Sibling slugs: single-word permalinks yield no variants (no extra calls)
    siblings = [v for v in slug_variants(permalink) if v != permalink]
    for p in await _probe_and_enrich(siblings):
        if p["soundcloud_permalink"] not in seen:
            seen.add(p["soundcloud_permalink"])
            results.append(p)
    return results


async def _resolve_exact(permalink: str) -> dict | None:
    """Resolve one permalink to a full suggestion entry (score 100)."""
    try:
        # Try oEmbed first (fast, no auth needed)
        oembed_url = (
            f"https://soundcloud.com/oembed?"
            f"url={__import__('urllib.parse', fromlist=['quote']).quote(f'https://soundcloud.com/{permalink}')}"
            f"&format=json"
        )
        client = _get_client()
        oembed_resp = await client.get(oembed_url, follow_redirects=True)
        oembed_data = oembed_resp.json() if oembed_resp.status_code == 200 else {}
    except Exception:
        oembed_data = {}

    name = oembed_data.get("author_name", permalink)
    avatar = oembed_data.get("thumbnail_url")

    # Resolve via v2 API for full user data.
    # username is the primary display name; full_name is the secondary
    # "real name" field and must not win (it breaks name-based linking).
    followers = None
    try:
        client = _get_client()
        client_id, app_version = await _creds.get(client)
        resolve_url = _with_creds(
            f"{API_URL}/resolve?url=https://soundcloud.com/{permalink}",
            client_id,
            app_version,
        )
        user = await _rate_limited_get(resolve_url)
        if user:
            if user.get("username"):
                name = user["username"]
            elif user.get("full_name"):
                name = user["full_name"]
            if user.get("avatar_url"):
                avatar = _upgrade_artwork(user["avatar_url"])
            followers = user.get("followers_count")
    except Exception:
        pass

    return {
        "soundcloud_permalink": permalink,
        "name": name,
        "disambiguation": "",
        "type": "",
        "country": "",
        "score": 100,
        "source": "soundcloud",
        "artistImageUrl": avatar or "",
        "followers_count": followers,
    }


def slug_variants(name: str) -> list[str]:
    """Generate SoundCloud permalink candidates from a display name.

    'six impala' -> ['siximpala', 'six-impala', 'six_impala'].
    """
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    if not words:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for candidate in ("".join(words), "-".join(words), "_".join(words)):
        if 1 <= len(candidate) <= 80 and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


async def _probe_permalink(permalink: str) -> dict | None:
    """Check whether a permalink exists via oEmbed (fast, no auth needed).

    Returns a suggestion entry, or None when the profile doesn't exist.
    """
    from urllib.parse import quote
    try:
        client = _get_client()
        url = (
            f"https://soundcloud.com/oembed?"
            f"url={quote(f'https://soundcloud.com/{permalink}')}"
            f"&format=json"
        )
        resp = await client.get(url, follow_redirects=True)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return {
            "soundcloud_permalink": permalink,
            "name": data.get("author_name", permalink),
            "disambiguation": "",
            "type": "",
            "country": "",
            "score": 90,
            "source": "soundcloud",
            "artistImageUrl": data.get("thumbnail_url") or "",
            "followers_count": None,
        }
    except Exception:
        return None


async def suggest_by_name(name: str) -> list[dict]:
    """Probe slug variants for a display name; return existing profiles.

    Variants are checked concurrently. Exact-score (100) entries are not
    produced here — suggestions score 90 so an exact permalink/URL hit
    always sorts first. Hits are enriched with full v2 user data
    (canonical username, avatar, follower count) so similar slugs can
    be told apart.
    """
    variants = slug_variants(name)
    if not variants:
        return []
    return await _probe_and_enrich(variants)


async def _probe_and_enrich(variants: list[str]) -> list[dict]:
    """oEmbed-probe variants for existence, then v2-enrich the hits."""
    if not variants:
        return []
    probed = await asyncio.gather(*[_probe_permalink(v) for v in variants])
    hits = [p for p in probed if p]
    if not hits:
        return []
    enriched = await asyncio.gather(*[_enrich_suggestion(p) for p in hits])
    return [e for e in enriched if e]


async def _enrich_suggestion(entry: dict) -> dict | None:
    """Fill a probed suggestion with v2 user data (username, avatar, followers)."""
    try:
        user = await _fetch_user(entry["soundcloud_permalink"])
    except Exception:
        return entry
    if not user:
        return entry
    if user.get("username"):
        entry["name"] = user["username"]
    avatar = _upgrade_artwork(user.get("avatar_url"))
    if avatar:
        entry["artistImageUrl"] = avatar
    entry["followers_count"] = user.get("followers_count")
    return entry


async def _fetch_user(permalink: str) -> dict | None:
    """Resolve a permalink to a full SoundCloud user object."""
    try:
        client = _get_client()
        client_id, app_version = await _creds.get(client)
        resolve_url = _with_creds(
            f"{API_URL}/resolve?url=https://soundcloud.com/{permalink}",
            client_id,
            app_version,
        )
        return await _rate_limited_get(resolve_url)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            # Retry once with fresh creds
            _creds.invalidate()
            try:
                client = _get_client()
                client_id, app_version = await _creds.get(client)
                resolve_url = _with_creds(
                    f"{API_URL}/resolve?url=https://soundcloud.com/{permalink}",
                    client_id,
                    app_version,
                )
                return await _rate_limited_get(resolve_url)
            except Exception:
                return None
        return None
    except Exception:
        return None


async def _fetch_user_tracks(user_id: int) -> list[dict]:
    """Fetch all tracks for a SoundCloud user (paginated)."""
    client = _get_client()
    client_id, app_version = await _creds.get(client)

    all_tracks: list[dict] = []
    next_href = _with_creds(
        f"{API_URL}/users/{user_id}/tracks?limit={PAGE_LIMIT}&offset=0",
        client_id,
        app_version,
    )

    for _ in range(MAX_PAGES):
        if not next_href:
            break
        try:
            data = await _rate_limited_get(next_href)
        except Exception:
            break
        for track in data.get("collection", []):
            all_tracks.append(track)
        next_href_raw = data.get("next_href")
        if next_href_raw:
            next_href = _with_creds(next_href_raw, client_id, app_version)
        else:
            break

    return all_tracks


def _track_to_release(track: dict) -> dict:
    """Convert a SoundCloud track to a release dict matching the app's format."""
    permalink_url = track.get("permalink_url", "")
    title = (track.get("title") or "").strip() or "Untitled"
    created_at = track.get("created_at", "")

    # Normalize date to YYYY-MM-DD
    date_str = ""
    if created_at:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d")
        except Exception:
            date_str = created_at[:10] if len(created_at) >= 10 else ""

    artwork = _upgrade_artwork(track.get("artwork_url"))
    user = track.get("user", {})
    permalink = track.get("permalink", "")

    return {
        "mbid": permalink or str(track.get("id", "")),
        "title": title,
        "type": "Single",
        "date": date_str,
        "url": permalink_url,
        "artwork_url": artwork or "",
        "soundcloud_track_id": str(track.get("id", "")),
        "credits": [],
    }


async def get_artist_releases(permalink: str) -> list[dict]:
    """Fetch all tracks for a SoundCloud user, returning each as a Single release."""
    user = await _fetch_user(permalink)
    if not user or not user.get("id"):
        return []

    tracks = await _fetch_user_tracks(user["id"])
    return [_track_to_release(t) for t in tracks]


async def get_release_tracks(track_id: str) -> list[dict]:
    """Fetch track info for a SoundCloud release.

    For SC, the release IS a single track, so we return a one-element list.
    """
    try:
        client = _get_client()
        client_id, app_version = await _creds.get(client)
        url = _with_creds(f"{API_URL}/tracks/{track_id}", client_id, app_version)
        track = await _rate_limited_get(url)
    except Exception:
        return []

    title = (track.get("title") or "").strip() or "Untitled"
    duration_ms = track.get("duration", 0)

    return [
        {
            "number": "1",
            "title": title,
            "length": duration_ms,
            "credits": [],
        }
    ]
