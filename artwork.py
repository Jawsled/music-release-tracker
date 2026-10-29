"""High-resolution album-artwork downloads.

The feed renders small covers: MusicBrainz through the disk-cached 250 px
Cover Art Archive proxy, and the other sources at whatever size their API
returned (Bandcamp ~700 px, SoundCloud 500 px, iTunes 600 px). Downloading
those same URLs gives a small, blurry file, so this module maps a stored
artwork URL (or release-group MBID) to the provider's larger variants,
fetches the closest one to the requested size, and reports the real
dimensions.

Only known artwork CDNs are fetched. The URL arrives from the browser, so an
allow-list keeps the endpoint from becoming a generic server-side request
proxy.
"""
from __future__ import annotations

import re
import struct
from typing import NamedTuple
from urllib.parse import quote, urlparse

import httpx

USER_AGENT = "MusicReleaseTracker/0.1.0 (https://github.com/placeholder)"
REQUEST_TIMEOUT = httpx.Timeout(25.0, connect=10.0)
MAX_BYTES = 40 * 1024 * 1024
MAX_ATTEMPTS = 4

# Hosts the providers actually serve artwork from. Matching is on the host
# itself or any subdomain of it.
ALLOWED_HOSTS = (
    "mzstatic.com",          # Apple Music / iTunes
    "sndcdn.com",            # SoundCloud
    "bcbits.com",            # Bandcamp
    "coverartarchive.org",   # MusicBrainz Cover Art Archive
    "archive.org",           # CAA originals redirect here
)

# The endpoint takes a source from the browser, so each source may only fetch
# from its own CDN. This keeps it from acting as a general-purpose proxy even
# for otherwise trusted hosts.
SOURCE_HOSTS = {
    "itunes": ("mzstatic.com",),
    "soundcloud": ("sndcdn.com",),
    "bandcamp": ("bcbits.com",),
    "musicbrainz": ("coverartarchive.org", "archive.org"),
}

_MBID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ITUNES_SIZE_RE = re.compile(
    r"/\d+x\d+(?:bb|sr|w|h)?(?=\.(?:jpe?g|png|webp)(?:\?|$))", re.I
)
_SOUNDCLOUD_SIZE_RE = re.compile(r"-t\d+x\d+(?=\.\w+(?:\?|$))", re.I)
_BANDCAMP_SIZE_RE = re.compile(
    r"_\d+(?=\.(?:jpe?g|png|webp)(?:\?|$))", re.I
)
_FILENAME_BAD_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}

_client: httpx.AsyncClient | None = None


class Artwork(NamedTuple):
    data: bytes
    content_type: str
    width: int | None
    height: int | None
    url: str


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
    return _client


# --- URL validation -------------------------------------------------------

def _host_matches(host: str, allowed: tuple[str, ...]) -> bool:
    return any(host == name or host.endswith("." + name) for name in allowed)


def _clean_url(url: str, hosts: tuple[str, ...] = ALLOWED_HOSTS) -> str:
    """Return the URL only when it points at one of the allowed hosts."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    host = parsed.hostname.lower().rstrip(".")
    if not _host_matches(host, hosts):
        return ""
    return url


def _infer_source(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for source, hosts in SOURCE_HOSTS.items():
        if _host_matches(host, hosts):
            return source
    return ""


# --- Provider variants ----------------------------------------------------

def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _mb_candidates(mbid: str, size: int | None) -> list[str]:
    if not mbid or not _MBID_RE.match(mbid):
        return []
    base = f"https://coverartarchive.org/release-group/{mbid.lower()}"
    # CAA only exposes 250/500/1200 thumbnails plus the untouched original.
    if size is None or size > 1200:
        return [f"{base}/front", f"{base}/front-1200", f"{base}/front-500"]
    if size > 500:
        return [f"{base}/front-1200", f"{base}/front-500", f"{base}/front-250"]
    if size > 250:
        return [f"{base}/front-500", f"{base}/front-250", f"{base}/front-1200"]
    return [f"{base}/front-250", f"{base}/front-500", f"{base}/front-1200"]


def _itunes_candidates(url: str, size: int | None) -> list[str]:
    # Apple's CDN accepts an arbitrary square size in the filename.
    target = size or 3000
    resized = _ITUNES_SIZE_RE.sub(f"/{target}x{target}bb", url, count=1)
    return _dedupe([resized, url])


def _soundcloud_candidates(url: str, size: int | None) -> list[str]:
    # SoundCloud only offers fixed transforms; -original is usually the
    # largest upload and -t500x500 is the biggest named thumbnail.
    original = _SOUNDCLOUD_SIZE_RE.sub("-original", url)
    large = _SOUNDCLOUD_SIZE_RE.sub("-t500x500", url)
    if size is None or size > 500:
        return _dedupe([original, large, url])
    return _dedupe([large, original, url])


def _bandcamp_candidates(url: str, size: int | None) -> list[str]:
    # Bandcamp image codes: _16 is ~700 px, _10 is ~1200 px.
    def with_code(code: str) -> str:
        return _BANDCAMP_SIZE_RE.sub(f"_{code}", url, count=1)

    if size is None or size > 700:
        return _dedupe([with_code("10"), with_code("16"), with_code("5"), url])
    return _dedupe([with_code("16"), with_code("10"), with_code("5"), url])


# --- Image inspection -----------------------------------------------------

def _sniff_content_type(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    return ""


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    i, length = 2, len(data)
    while i + 9 < length:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        while marker == 0xFF and i + 2 < length:  # fill bytes
            i += 1
            marker = data[i + 1]
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > length:
            break
        segment_length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if segment_length < 2:
            break
        if marker in _SOF_MARKERS:
            if i + 9 <= length:
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return width, height
            break
        i += 2 + segment_length
    return None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Best-effort (width, height) from PNG/GIF/WebP/JPEG bytes."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", data[6:10])
        return width, height
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
        if chunk == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 " and len(data) >= 30:
            width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return width, height
    return _jpeg_dimensions(data)


# --- Fetch ----------------------------------------------------------------

async def _fetch(url: str) -> tuple[bytes, str] | None:
    """Download one image, enforcing the size cap. None on any failure."""
    try:
        async with _get_client().stream("GET", url) as resp:
            if resp.status_code != 200:
                return None
            declared = (resp.headers.get("content-type") or "").split(";")[0]
            declared = declared.strip().lower()
            if declared and not declared.startswith("image/") and declared not in (
                "application/octet-stream", "binary/octet-stream",
            ):
                return None
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    return None
                chunks.append(chunk)
    except Exception:
        return None
    data = b"".join(chunks)
    if not data:
        return None
    content_type = _sniff_content_type(data)
    if not content_type:
        return None
    return data, content_type


async def resolve(
    source: str,
    mbid: str = "",
    artwork_url: str = "",
    size: int | None = 1000,
) -> Artwork | None:
    """Fetch the artwork variant closest to `size` (None = largest available).

    Candidates are tried in provider-specific order. The first image that is
    at least the requested size wins; otherwise the closest smaller image is
    kept. Providers that can generate any size (iTunes) need one request.
    """
    source = (source or "").strip().lower()
    raw_url = (artwork_url or "").strip()
    artwork_url = _clean_url(raw_url)
    if not source and artwork_url:
        source = _infer_source(artwork_url)
    # Re-check against the source's own CDN; a mismatched URL is ignored.
    artwork_url = _clean_url(raw_url, SOURCE_HOSTS.get(source, ()))

    if source == "musicbrainz" or (not source and mbid):
        urls = _mb_candidates(mbid, size)
    elif source == "itunes" and artwork_url:
        urls = _itunes_candidates(artwork_url, size)
    elif source == "soundcloud" and artwork_url:
        urls = _soundcloud_candidates(artwork_url, size)
    elif source == "bandcamp" and artwork_url:
        urls = _bandcamp_candidates(artwork_url, size)
    else:
        return None

    best: Artwork | None = None
    best_short = 0
    # When the first candidate is already the provider's largest variant,
    # there is nothing larger to compare against: accept the first success.
    largest_first = (
        source == "itunes"
        or (source == "musicbrainz" and size is not None and size > 1200)
        or (source == "soundcloud" and size is not None and size > 500)
        or (source == "bandcamp" and size is not None and size > 700)
    )
    for url in urls[:MAX_ATTEMPTS]:
        fetched = await _fetch(url)
        if not fetched:
            continue
        data, content_type = fetched
        dims = image_dimensions(data)
        if dims is None:
            # Unknown container: take the first usable image rather than
            # guessing at its size.
            if best is None:
                return Artwork(data, content_type, None, None, url)
            continue

        width, height = dims
        short = min(width, height)
        if size is None or largest_first:
            return Artwork(data, content_type, width, height, url)

        score = abs(short - size)
        if best is None or score < abs(best_short - size) or (
            score == abs(best_short - size) and short > best_short
        ):
            best = Artwork(data, content_type, width, height, url)
            best_short = short
        if short >= size:
            break

    return best


# --- Download response helpers -------------------------------------------

def safe_filename(value: str, limit: int = 120) -> str:
    value = _FILENAME_BAD_RE.sub("", value or "")
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value[:limit].strip(" .") or "album-art"
    if value.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
        r"(?:COM|LPT)[1-9]", value.upper()
    ):
        value = "_" + value
    return value


def build_filename(
    artist: str,
    title: str,
    width: int | None,
    height: int | None,
    content_type: str,
) -> str:
    extension = {
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    }.get(content_type, "jpg")
    base = safe_filename(" - ".join(p for p in (artist, title) if p and p.strip()))
    dimensions = f"{width}x{height}" if width and height else "artwork"
    return f"{base} ({dimensions}).{extension}"


def content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.replace('"', "").replace("\\", "") or "album-art.jpg"
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
