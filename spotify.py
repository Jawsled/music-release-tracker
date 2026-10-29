"""Spotify link helpers.

Spotify is not a tracked source: the app never stores or scans it. Artist links
are only used as an unambiguous input to locate the matching MusicBrainz artist
through MusicBrainz URL relationships (the same idea behind SAMBL), so a pasted
link resolves to the right artist instead of being treated as a name search.

The Spotify web page is also used as a fallback for artists whose MusicBrainz
entry has no Spotify URL relationship yet: its embedded metadata gives the real
artist name, which is a much better query than the raw link.
"""
from __future__ import annotations

import html
import json
import re

import httpx

# The public artist page is server-rendered for lightweight clients. A full
# desktop-browser UA gets the JS web-player shell instead (no artist metadata),
# so keep this deliberately simple.
USER_AGENT = "Mozilla/5.0"

_SPOTIFY_HOST_RE = re.compile(r"(?:^|//)(?:open\.|play\.)?spotify\.com/", re.I)
_SHORT_LINK_RE = re.compile(r"https?://(?:spotify\.link|spoti\.fi)/\S+", re.I)
_ARTIST_ID_RE = re.compile(
    r"(?:(?:^|//)(?:open\.|play\.)?spotify\.com/"
    r"(?:intl-[A-Za-z-]+/)?(?:embed/)?artist/"
    r"|spotify:artist:)([A-Za-z0-9]{22})",
    re.I,
)
_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
    re.I,
)


def looks_like_spotify(text: str) -> bool:
    """True when the query is a Spotify URL, short link or spotify: URI."""
    text = (text or "").strip()
    return bool(
        _SPOTIFY_HOST_RE.search(text)
        or _SHORT_LINK_RE.search(text)
        or text.lower().startswith("spotify:")
    )


def extract_artist_id(text: str) -> str | None:
    """Extract the 22-character artist ID from a Spotify artist URL/URI."""
    match = _ARTIST_ID_RE.search(text or "")
    return match.group(1) if match else None


def artist_url(artist_id: str) -> str:
    """Canonical Spotify URL, as stored in MusicBrainz URL relationships."""
    return f"https://open.spotify.com/artist/{artist_id}"


async def _get(url: str) -> httpx.Response:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={
            "User-Agent": USER_AGENT,
        },
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response


async def resolve_artist_id(text: str) -> str | None:
    """Resolve a Spotify artist link (including spotify.link/spoti.fi) to its ID.

    Returns None when the text is not a Spotify artist link, when a short link
    cannot be followed, or when it points at an album/track/etc.
    """
    artist_id = extract_artist_id(text)
    if artist_id:
        return artist_id
    short = _SHORT_LINK_RE.search(text or "")
    if not short:
        return None
    try:
        response = await _get(short.group(0))
    except Exception:
        return None
    return extract_artist_id(str(response.url))


def _json_ld_artist_name(page: str) -> str | None:
    """Read the artist name from the page's schema.org MusicGroup/Person block."""
    for block in _JSON_LD_RE.findall(page):
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if not any(k in ("MusicGroup", "Person", "MusicArtist") for k in kinds):
                continue
            name = (item.get("name") or "").strip()
            if name:
                return name
    return None


async def fetch_artist_name(artist_id: str) -> str | None:
    """Return the artist's display name from their public Spotify artist page."""
    response = await _get(artist_url(artist_id))
    if "/login" in str(response.url):
        return None
    page = response.text

    name = _json_ld_artist_name(page)
    if not name:
        match = _OG_TITLE_RE.search(page)
        if match:
            name = html.unescape(match.group(1)).strip()
    if not name or name.lower() in ("spotify", "spotify – web player"):
        return None
    return name
