"""Artist-link parsers for streaming services that are not tracked sources.

Spotify lives in :mod:`spotify` because it has a specialized page-name
fallback. This module covers the other services the app already lists on
release pages — Tidal, Qobuz, Amazon Music and YouTube/YouTube Music — and maps
their various link shapes onto the canonical URL stored in MusicBrainz artist
URL relationships. The actual resolution is done by
:func:`musicbrainz.get_artists_by_url`; nothing here talks to those services
except the optional page-title fallback used when MB has no link yet.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

import httpx

# Same lightweight UA as spotify.py: server-rendered pages instead of the JS
# web-player shells that carry no metadata.
USER_AGENT = "Mozilla/5.0"

_TIDAL_RE = re.compile(
    r"(?:^|//)(?:listen\.)?tidal\.com/(?:browse/)?artist/(\d+)", re.I
)
_QOBUZ_RE = re.compile(
    r"(?:^|//)(?:open\.|play\.|www\.)?qobuz\.com/"
    r"(?:[A-Za-z]{2}-[A-Za-z]{2}/)?(?:artist|interpreter)/"
    r"(?:[^/?#]+/)?(\d+)",
    re.I,
)
_AMAZON_RE = re.compile(
    r"(?:^|//)music\.amazon\.([A-Za-z.]+)/artists/([A-Za-z0-9]+)", re.I
)
_YT_MUSIC_CHANNEL_RE = re.compile(
    r"(?:^|//)music\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})", re.I
)
_YT_CHANNEL_RE = re.compile(
    r"(?:^|//)(?:www\.|m\.)?youtube\.com/channel/(UC[A-Za-z0-9_-]{22})", re.I
)
_YT_HANDLE_RE = re.compile(
    r"youtube\.com/((?:@|c/|user/)[^\s?#]+)", re.I
)
_YT_CANONICAL_RE = re.compile(
    r"<link[^>]+rel=[\"']canonical[\"'][^>]*>", re.I
)
_YT_CHANNEL_HREF_RE = re.compile(
    r"youtube\.com/channel/(UC[A-Za-z0-9_-]{22})", re.I
)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
    re.I,
)
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)

_HOST_PATTERNS = (
    re.compile(r"(?:^|//)(?:listen\.)?tidal\.com/", re.I),
    re.compile(r"(?:^|//)(?:open\.|play\.|www\.)?qobuz\.com/", re.I),
    re.compile(r"(?:^|//)music\.amazon\.", re.I),
    re.compile(r"(?:^|//)(?:www\.|m\.|music\.)?youtube\.com/", re.I),
    re.compile(r"(?:^|//)youtu\.be/", re.I),
)

_TITLE_SUFFIXES = (
    " - YouTube Music",
    " - YouTube",
    " on TIDAL",
    " | TIDAL",
    " - Topic",
)


@dataclass(frozen=True)
class ArtistLink:
    """A parsed link plus the canonical URL candidate(s) MusicBrainz stores."""

    service: str
    label: str
    urls: tuple[str, ...]
    name_url: str | None = None


def looks_like(text: str) -> bool:
    """True when the query is a link for one of the supported services."""
    text = (text or "").strip()
    return any(pattern.search(text) for pattern in _HOST_PATTERNS)


async def _get(url: str) -> httpx.Response:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={"User-Agent": USER_AGENT},
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response


async def _youtube_channel_from_handle(url: str) -> str | None:
    """Resolve an @handle / /c/ / /user/ URL to its channel ID via YouTube."""
    try:
        response = await _get(url)
    except Exception:
        return None
    match = _YT_CANONICAL_RE.search(response.text)
    if not match:
        return None
    channel = _YT_CHANNEL_HREF_RE.search(match.group(0))
    return channel.group(1) if channel else None


async def parse(text: str) -> ArtistLink | None:
    """Parse a supported artist link, or return None for albums/videos/etc."""
    text = (text or "").strip()

    match = _TIDAL_RE.search(text)
    if match:
        artist_id = match.group(1)
        return ArtistLink(
            "tidal",
            "Tidal",
            (
                f"https://tidal.com/artist/{artist_id}",
                f"https://listen.tidal.com/artist/{artist_id}",
            ),
            f"https://tidal.com/artist/{artist_id}",
        )

    match = _QOBUZ_RE.search(text)
    if match:
        artist_id = match.group(1)
        return ArtistLink(
            "qobuz",
            "Qobuz",
            (
                f"https://open.qobuz.com/artist/{artist_id}",
                f"https://play.qobuz.com/artist/{artist_id}",
            ),
        )

    match = _AMAZON_RE.search(text)
    if match:
        tld, artist_id = match.group(1).lower(), match.group(2)
        urls = [f"https://music.amazon.{tld}/artists/{artist_id}"]
        # MB editors often record the .com URL even when the link was copied
        # from another storefront; the numeric/ASIN ID is often (not always)
        # shared across locales, so try it as a cheap second candidate.
        if tld != "com":
            urls.append(f"https://music.amazon.com/artists/{artist_id}")
        return ArtistLink("amazon_music", "Amazon Music", tuple(urls))

    match = _YT_MUSIC_CHANNEL_RE.search(text)
    if match:
        url = f"https://music.youtube.com/channel/{match.group(1)}"
        return ArtistLink("youtube_music", "YouTube Music", (url,), url)

    match = _YT_CHANNEL_RE.search(text)
    if match:
        channel_id = match.group(1)
        return ArtistLink(
            "youtube",
            "YouTube",
            (
                f"https://www.youtube.com/channel/{channel_id}",
                f"https://youtube.com/channel/{channel_id}",
            ),
            f"https://www.youtube.com/channel/{channel_id}",
        )

    # @handle / /c/ / /user/ links need one YouTube round-trip to get the ID.
    match = _YT_HANDLE_RE.search(text)
    if match:
        channel_id = await _youtube_channel_from_handle(
            f"https://www.youtube.com/{match.group(1)}"
        )
        if channel_id:
            return ArtistLink(
                "youtube",
                "YouTube",
                (
                    f"https://www.youtube.com/channel/{channel_id}",
                    f"https://youtube.com/channel/{channel_id}",
                ),
                f"https://www.youtube.com/channel/{channel_id}",
            )

    return None


def _clean_title(title: str) -> str | None:
    title = html.unescape(title or "").strip()
    for suffix in _TITLE_SUFFIXES:
        if title.lower().endswith(suffix.lower()):
            title = title[: -len(suffix)].strip()
    if not title or title.lower() in (
        "youtube", "youtube music", "tidal", "qobuz", "amazon music"
    ):
        return None
    return title


def _json_ld_name(page: str) -> str | None:
    for block in _JSON_LD_RE.findall(page):
        match = re.search(r'"name"\s*:\s*"([^"]+)"', block)
        if match:
            return match.group(1)
    return None


async def fetch_artist_name(link: ArtistLink) -> str | None:
    """Read the artist name from the service page (for the name-match fallback)."""
    if not link.name_url:
        return None
    response = await _get(link.name_url)
    page = response.text
    for pattern in (_OG_TITLE_RE, _HTML_TITLE_RE):
        match = pattern.search(page)
        if match:
            name = _clean_title(match.group(1))
            if name:
                return name
    return _clean_title(_json_ld_name(page) or "")
