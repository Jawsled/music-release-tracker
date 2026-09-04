from __future__ import annotations

import asyncio
import json
import logging
import webbrowser
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import db
import musicbrainz
import itunes
import soundcloud
import logstore

HOST = "127.0.0.1"
PORT = 7070

# Setup application logging
logger = logging.getLogger("music-release-tracker")
logger.setLevel(logging.INFO)

# Capture library-level logs (retries, timeouts, rate limits) into the UI log
logstore.attach_handler("music-release-tracker")


def _add_log(level: str, message: str, artist: str = "", detail: str = ""):
    """Add a log entry to the shared UI log store (also mirrors to console)."""
    logstore.add_log(level, message, artist=artist, detail=detail)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    backup_path = db.backup_database()
    if backup_path:
        _add_log("INFO", f"Database backup saved to {backup_path}")
    try:
        counts = db.get_counts()
        _add_log("INFO", f"Using database {db.DB_PATH} "
                 f"({counts['artists']} artists, {counts['releases']} releases)")
    except Exception as e:
        _add_log("ERROR", "Could not read database stats", detail=str(e))
    # Run startup dedup if enabled
    settings = db.get_all_settings()
    if settings.get("startup_dedup") == "1":
        _add_log("INFO", "Running startup duplicate check...")
        hidden = _remove_startup_duplicates()
        if hidden:
            _add_log("INFO", f"Hidden {hidden} duplicate release(s)")
        else:
            _add_log("INFO", "No duplicates found")
    # Check all artists for new releases if enabled (background, non-blocking)
    if settings.get("startup_refresh") == "1":
        asyncio.create_task(_startup_refresh())
    # Retroactively fetch tracklists for MB albums/EPs missing track_titles
    asyncio.create_task(_backfill_tracklists())
    webbrowser.open(f"http://{HOST}:{PORT}")
    yield


async def _startup_refresh():
    """Background scan for new releases at startup (no SSE client attached).

    Mirrors the /api/check logic: one pass over all artists plus a single
    retry pass for transient failures. Progress lands in the Scan Logs.
    """
    artists = db.get_all_artists()
    if not artists:
        return
    _add_log("INFO", f"Startup refresh: checking {len(artists)} artist(s) for new releases...")
    total_new = 0
    failed: list[dict] = []
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def run_one(artist: dict) -> tuple[dict, list[str], bool]:
        async with sem:
            try:
                return artist, await _fetch_artist_new_titles(artist), True
            except Exception as e:
                _add_log("ERROR", "Startup refresh failed, will retry at the end",
                         artist=artist["name"], detail=_error_message(e))
                failed.append(artist)
                return artist, [], False

    async def run_retry(artist: dict) -> tuple[dict, list[str], bool]:
        try:
            return artist, await _fetch_artist_new_titles(artist), True
        except Exception as e:
            _add_log("ERROR", "Startup refresh failed again, skipping",
                     artist=artist["name"], detail=_error_message(e))
            return artist, [], False

    for coro in asyncio.as_completed([run_one(a) for a in artists]):
        artist, titles, ok = await coro
        if not ok:
            continue
        _add_log("INFO", f"Checked OK - {len(titles)} new release(s)", artist=artist["name"])
        total_new += len(titles)
    for artist in list(failed):
        _, titles, ok = await run_retry(artist)
        if not ok:
            continue
        failed.remove(artist)
        _add_log("INFO", f"Checked OK on retry - {len(titles)} new release(s)", artist=artist["name"])
        total_new += len(titles)
    _add_log("INFO", f"Startup refresh complete. Found {total_new} new release(s).")


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Workaround for Jinja2 3.1.x cache bug: use a simple dict cache instead of the broken LRU cache
from jinja2 import Environment, FileSystemLoader
templates = Jinja2Templates(directory="templates")
templates.env.cache = {}  # Replace broken LRU cache with simple dict


# --- Request models ---

class ArtistSearchRequest(BaseModel):
    query: str
    source: str = ""  # empty = search both
    sources: list[str] | None = None  # platform filter; None = all three

class ArtistAddRequest(BaseModel):
    source: str  # "musicbrainz", "itunes", or "soundcloud"
    id: str  # mbid for musicbrainz, artistId for itunes, permalink for soundcloud
    name: str
    disambiguation: str = ""

class iTunesArtistSearchRequest(BaseModel):
    query: str
    country: str = "us"


# --- Pages ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# --- Artist endpoints ---

@app.get("/api/artists")
async def list_artists():
    return db.get_all_artists()


@app.post("/api/artists/search")
async def search_artists(body: ArtistSearchRequest):
    """Search both MusicBrainz and iTunes in parallel, merge by name.
    
    Also supports pasting MBIDs, MusicBrainz URLs, or iTunes/Apple Music URLs
    directly into the search bar.
    
    Returns combined results where matching names are merged into single entries
    with both mbid and itunes_artist_id populated.
    """
    import re as _re

    # Check if query is a URL or MBID paste
    query = body.query.strip()
    
    # Try to parse MBID directly (UUID format)
    mbid_match = _re.match(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', query)
    if mbid_match:
        # It's a raw MBID - look up the artist
        try:
            data = await musicbrainz._rate_limited_get(
                f"{musicbrainz.BASE_URL}/artist/{query}",
                params={"fmt": "json"},
            )
            return [{
                "mbid": data["id"],
                "name": data.get("name", ""),
                "disambiguation": data.get("disambiguation", ""),
                "type": data.get("type", ""),
                "country": data.get("country", ""),
                "score": 100,
                "source": "musicbrainz",
            }]
        except Exception:
            return []

    # Try to parse MusicBrainz URL
    mb_url_match = _re.search(r'musicbrainz\.org/artist/([0-9a-fA-F-]{36})', query)
    if mb_url_match:
        mbid = mb_url_match.group(1)
        try:
            data = await musicbrainz._rate_limited_get(
                f"{musicbrainz.BASE_URL}/artist/{mbid}",
                params={"fmt": "json"},
            )
            return [{
                "mbid": data["id"],
                "name": data.get("name", ""),
                "disambiguation": data.get("disambiguation", ""),
                "type": data.get("type", ""),
                "country": data.get("country", ""),
                "score": 100,
                "source": "musicbrainz",
            }]
        except Exception:
            return []

    # Try to parse iTunes/Apple Music URL
    itunes_url_match = _re.search(r'(?:music\.apple|itunes\.apple)\.com/[^/]+/artist/[^/]+/(\d+)', query)
    if itunes_url_match:
        itunes_id = int(itunes_url_match.group(1))
        try:
            lookup_data = await itunes._rate_limited_get(
                "https://itunes.apple.com/lookup",
                params={"id": itunes_id, "country": "us"},
            )
            results = lookup_data.get("results", [])
            if results:
                a = results[0]
                return [{
                    "artistId": a.get("artistId"),
                    "name": a.get("artistName", ""),
                    "disambiguation": a.get("primaryGenreName", ""),
                    "country": "us",
                    "score": 100,
                    "source": "itunes",
                }]
        except Exception:
            pass

    # SoundCloud URL pastes resolve to the exact profile (unambiguous link).
    # Plain words / spaced names fall through to the combined search below,
    # which probes all selected platforms in parallel.
    if body.source not in ("musicbrainz", "itunes"):
        sc_url_match = _re.search(
            r'(?:www\.|m\.)?soundcloud\.com/([A-Za-z0-9._-]+)', query
        )
        sc_short_match = _re.match(r'^https?://on\.soundcloud\.com/', query)

        if sc_url_match or sc_short_match:
            try:
                return _mark_sc_tracked(await soundcloud.search_artist(query))
            except Exception:
                pass

    # Regular search
    # Load existing artists to check what's already tracked
    existing_artists = db.get_all_artists()
    # Build lookup: normalized_name -> {mbid, itunes_artist_id}
    existing_by_name: dict[str, dict] = {}
    for a in existing_artists:
        key = _normalize_name(a["name"])
        existing_by_name[key] = a

    async def _search_mb():
        results = await musicbrainz.search_artist(body.query)
        for r in results:
            r["source"] = "musicbrainz"
        return results

    async def _search_itunes():
        results = await itunes.search_artist(body.query)
        for r in results:
            r["itunes_artist_id"] = r.pop("artistId", None)
            r["source"] = "itunes"
        return results

    enabled = _enabled_sources()

    if body.source == "itunes":
        if "itunes" not in enabled:
            return []
        return await _search_itunes()
    elif body.source == "musicbrainz":
        if "musicbrainz" not in enabled:
            return []
        return await _search_mb()
    elif body.source == "soundcloud":
        if "soundcloud" not in enabled:
            return []
        try:
            return _mark_sc_tracked(await soundcloud.search_artist(query))
        except Exception as e:
            import logging
            logging.warning(f"SoundCloud search failed for '{query}': {e}")
        return []
    else:
        # Combined search across selected platforms (platform filter UI)
        # intersected with globally enabled sources. A bare permalink-like
        # word or spaced name also probes SoundCloud here instead of
        # short-circuiting it, so MB/iTunes results are never shadowed.
        wanted = {s.lower() for s in (body.sources or ["musicbrainz", "itunes", "soundcloud"])}
        wanted &= {"musicbrainz", "itunes", "soundcloud"}
        if not wanted:
            wanted = {"musicbrainz", "itunes", "soundcloud"}
        use = {s for s in wanted if s in enabled}
        if not use:
            return []

        async def _search_sc():
            try:
                return await soundcloud.search_artist(query)
            except Exception:
                return []

        coros = {}
        if "musicbrainz" in use:
            coros["musicbrainz"] = _search_mb()
        if "itunes" in use:
            coros["itunes"] = _search_itunes()
        if "soundcloud" in use:
            coros["soundcloud"] = _search_sc()
        gathered = await asyncio.gather(*coros.values())
        by_source = dict(zip(coros.keys(), gathered))
        mb_results = by_source.get("musicbrainz", [])
        it_results = by_source.get("itunes", [])
        sc_results = by_source.get("soundcloud", [])

        # Build lookup maps by normalized name
        mb_by_name: dict[str, dict] = {}
        for r in mb_results:
            key = _normalize_name(r["name"])
            mb_by_name[key] = r

        it_by_name: dict[str, dict] = {}
        for r in it_results:
            key = _normalize_name(r["name"])
            it_by_name[key] = r

        sc_by_name: dict[str, dict] = {}
        for r in sc_results:
            key = _normalize_name(r["name"])
            # First hit wins; exact slug matches come first from the prober
            sc_by_name.setdefault(key, r)

        # Merge: combine matching names, keep all unique names
        merged: dict[str, dict] = {}
        for key, r in mb_by_name.items():
            merged[key] = {
                "name": r["name"],
                "mbid": r.get("mbid", ""),
                "itunes_artist_id": None,
                "soundcloud_permalink": None,
                "disambiguation": r.get("disambiguation", ""),
                "type": r.get("type", ""),
                "country": r.get("country", ""),
                "score": r.get("score", 0),
                "source": "musicbrainz",
                "artistImageUrl": "",
            }

        for key, r in it_by_name.items():
            if key in merged:
                # Name match: merge into existing entry
                merged[key]["itunes_artist_id"] = r.get("itunes_artist_id")
                if merged[key]["source"] == "musicbrainz":
                    merged[key]["source"] = "both"
                # Preserve iTunes country if not already set
                if not merged[key].get("country") and r.get("country"):
                    merged[key]["country"] = r["country"]
                # Use iTunes image if available
                if r.get("artistImageUrl"):
                    merged[key]["artistImageUrl"] = r["artistImageUrl"]
            else:
                merged[key] = {
                    "name": r["name"],
                    "mbid": "",
                    "itunes_artist_id": r.get("itunes_artist_id"),
                    "soundcloud_permalink": None,
                    "disambiguation": r.get("disambiguation", ""),
                    "type": "",
                    "country": r.get("country", ""),
                    "score": r.get("score", 0),
                    "source": "itunes",
                    "artistImageUrl": r.get("artistImageUrl", ""),
                }

        for key, r in sc_by_name.items():
            if key in merged:
                # Name match: link the SoundCloud profile into the entry
                merged[key]["soundcloud_permalink"] = r.get("soundcloud_permalink")
                if merged[key]["source"] in ("musicbrainz", "itunes", "both"):
                    merged[key]["source"] += "+sc"
                if r.get("artistImageUrl") and not merged[key].get("artistImageUrl"):
                    merged[key]["artistImageUrl"] = r["artistImageUrl"]
                if r.get("followers_count") is not None:
                    merged[key]["followers_count"] = r["followers_count"]
                # Prefer the higher score for ranking
                merged[key]["score"] = max(merged[key].get("score", 0), r.get("score", 0))
            else:
                merged[key] = {
                    "name": r["name"],
                    "mbid": "",
                    "itunes_artist_id": None,
                    "soundcloud_permalink": r.get("soundcloud_permalink"),
                    "disambiguation": r.get("disambiguation", ""),
                    "type": r.get("type", ""),
                    "country": r.get("country", ""),
                    "score": r.get("score", 0),
                    "source": "soundcloud",
                    "artistImageUrl": r.get("artistImageUrl", ""),
                    "followers_count": r.get("followers_count"),
                }

        # Add already_tracked info for UI
        result = []
        for r in merged.values():
            key = _normalize_name(r["name"])
            existing = existing_by_name.get(key)
            r["already_tracked"] = existing is not None
            r["existing_mbid"] = existing["mbid"] if existing else None
            r["existing_itunes_id"] = existing.get("itunes_artist_id") if existing else None
            result.append(r)

        # Sort by score descending
        result.sort(key=lambda x: x.get("score", 0), reverse=True)
        return result


def _normalize_name(name: str) -> str:
    """Normalize artist name for fuzzy matching."""
    import re
    # Lowercase, strip punctuation/special chars, collapse whitespace
    name = name.lower().strip()
    name = re.sub(r'[^\w\s]', '', name)
    name = re.sub(r'\s+', ' ', name)
    return name


def _normalize_release_title(title: str) -> str:
    """Normalize release title for duplicate detection.

    Uses user-configured ignore suffixes (defaults: ' - Single', ' - EP',
    i.e. Apple Music classification suffixes that aren't part of the title).
    """
    return db.normalize_release_title(title)


def _mark_sc_tracked(results: list[dict]) -> list[dict]:
    """Annotate every SoundCloud result with already_tracked info for the UI."""
    for r in results:
        existing = db.get_artist_by_soundcloud_permalink(r.get("soundcloud_permalink", ""))
        if existing:
            r["already_tracked"] = True
            r["existing_mbid"] = existing.get("mbid", "")
            r["existing_itunes_id"] = existing.get("itunes_artist_id")
        else:
            r["already_tracked"] = False
    return results


@app.post("/api/itunes/search")
async def search_itunes_artists(body: iTunesArtistSearchRequest):
    """Search for artists specifically on iTunes."""
    results = await itunes.search_artist(body.query, country=body.country)
    return results


@app.get("/api/itunes/album-count")
async def itunes_album_count(artist_id: int = Query(...), country: str = Query("us")):
    """Return an artist's album count (for telling same-name artists apart)."""
    try:
        albums, capped = await itunes.get_artist_album_count(artist_id, country=country or "us")
    except Exception as e:
        return {"artist_id": artist_id, "albums": None, "capped": False, "error": _error_message(e)}
    return {"artist_id": artist_id, "albums": albums, "capped": capped}


@app.post("/api/artists")
async def add_artist(body: ArtistAddRequest):
    """Add or link an artist.
    
    If artist already exists by name match, links the new source ID to existing entry.
    Otherwise creates a new artist entry.
    """
    import re

    def normalize(name: str) -> str:
        name = name.lower().strip()
        name = re.sub(r'[^\w\s]', '', name)
        name = re.sub(r'\s+', ' ', name)
        return name

    # Check if artist already exists by exact ID match
    existing_by_id = None
    if body.source == "musicbrainz":
        existing_by_id = db.get_artist_by_mbid(body.id)
    elif body.source == "itunes":
        existing_by_id = db.get_artist_by_itunes_id(int(body.id))
    elif body.source == "soundcloud":
        existing_by_id = db.get_artist_by_soundcloud_permalink(body.id)

    if existing_by_id:
        return {"status": "already_exists", "artist": existing_by_id}

        # Check if artist exists by name match (for linking)
    all_artists = db.get_all_artists()
    name_key = normalize(body.name)
    existing_by_name = None
    for a in all_artists:
        if normalize(a["name"]) == name_key:
            existing_by_name = a
            break
    
    if existing_by_name:
        # Link the new source ID to existing artist
        artist = db.link_artist(
            artist_id=existing_by_name["id"],
            mbid=body.id if body.source == "musicbrainz" else existing_by_name.get("mbid", ""),
            itunes_artist_id=int(body.id) if body.source == "itunes" else existing_by_name.get("itunes_artist_id"),
            soundcloud_permalink=body.id if body.source == "soundcloud" else existing_by_name.get("soundcloud_permalink"),
        )

        # Import releases for the linked source
        count = 0
        if body.source == "musicbrainz":
            releases = await musicbrainz.get_artist_releases(body.id)
            for rel in releases:
                inserted = db.add_release(
                    mbid=rel["mbid"],
                    artist_id=artist["id"],
                    title=rel["title"],
                    release_type=rel["type"],
                    release_date=rel["date"],
                    notified=1,
                    source="musicbrainz",
                    mb_url=rel.get("url", ""),
                    credits=json.dumps(rel.get("credits") or []),
                )
                if inserted:
                    count += 1
        elif body.source == "itunes":
            releases = await itunes.get_artist_releases(int(body.id), artist_name=body.name)
            # Get existing MB releases for this artist to check for duplicates
            existing_releases = db.get_releases(artist_id=artist["id"])
            mb_titles = {_normalize_release_title(r["title"]) for r in existing_releases if r.get("source") == "musicbrainz"}
            for rel in releases:
                # Skip iTunes releases that duplicate MB releases (ignoring - Single/- EP suffix)
                itunes_title_norm = _normalize_release_title(rel["title"])
                if itunes_title_norm in mb_titles:
                    continue
                inserted = db.add_release(
                    mbid=str(rel["id"]),
                    artist_id=artist["id"],
                    title=rel["title"],
                    release_type=rel["type"],
                    release_date=rel.get("date", ""),
                    notified=1,
                    source="itunes",
                    itunes_collection_id=str(rel["id"]),
                    artwork_url=rel.get("artwork_url", ""),
                )
                if inserted:
                    count += 1
        elif body.source == "soundcloud":
            releases = await soundcloud.get_artist_releases(body.id)
            # Deduplicate: same as iTunes — exact normalized title set lookup.
            existing_releases = db.get_releases(artist_id=artist["id"])
            existing_titles = {_normalize_release_title(r["title"]) for r in existing_releases}
            for rel in releases:
                sc_title_norm = _normalize_release_title(rel["title"])
                if sc_title_norm in existing_titles:
                    continue
                inserted = db.add_release(
                    mbid=rel["mbid"],
                    artist_id=artist["id"],
                    title=rel["title"],
                    release_type=rel["type"],
                    release_date=rel.get("date", ""),
                    notified=1,
                    source="soundcloud",
                    soundcloud_track_id=rel.get("soundcloud_track_id", ""),
                    artwork_url=rel.get("artwork_url", ""),
                )
                if inserted:
                    count += 1

        return {"status": "linked", "artist": artist, "releases_imported": count}

    # Create new artist entry
    artist = db.add_artist(
        mbid=body.id if body.source == "musicbrainz" else "",
        name=body.name,
        disambiguation=body.disambiguation,
        itunes_artist_id=int(body.id) if body.source == "itunes" else None,
        soundcloud_permalink=body.id if body.source == "soundcloud" else None,
    )

    # Import releases based on source
    count = 0
    if body.source == "musicbrainz":
        releases = await musicbrainz.get_artist_releases(body.id)
        for rel in releases:
            inserted = db.add_release(
                mbid=rel["mbid"],
                artist_id=artist["id"],
                title=rel["title"],
                release_type=rel["type"],
                release_date=rel["date"],
                notified=1,
                source="musicbrainz",
                mb_url=rel.get("url", ""),
                credits=json.dumps(rel.get("credits") or []),
            )
            if inserted:
                count += 1
                # Fetch and store tracklist for albums/EPs (used for SC dedup)
                if rel["type"] in ("Album", "EP"):
                    try:
                        tracks = await musicbrainz.get_release_tracks(rel["mbid"], body.id)
                        if tracks:
                            db.update_release_track_titles(
                                db.get_release_by_mbid(rel["mbid"], artist["id"])["id"],
                                [t["title"] for t in tracks],
                            )
                    except Exception:
                        pass
    elif body.source == "itunes":
        releases = await itunes.get_artist_releases(int(body.id), artist_name=body.name)
        for rel in releases:
            inserted = db.add_release(
                mbid=str(rel["id"]),
                artist_id=artist["id"],
                title=rel["title"],
                release_type=rel["type"],
                release_date=rel.get("date", ""),
                notified=1,
                source="itunes",
                itunes_collection_id=str(rel["id"]),
                artwork_url=rel.get("artwork_url", ""),
            )
            if inserted:
                count += 1
    elif body.source == "soundcloud":
        releases = await soundcloud.get_artist_releases(body.id)
        # Deduplicate: check against existing release titles AND album/EP tracklists
        existing_releases = db.get_releases(artist_id=artist["id"])
        existing_titles = {_normalize_release_title(r["title"]) for r in existing_releases}
        album_track_titles = db.get_artist_all_track_titles(artist["id"])
        for rel in releases:
            sc_title_norm = _normalize_release_title(rel["title"])
            if sc_title_norm in existing_titles:
                continue
            if sc_title_norm in album_track_titles:
                continue
            inserted = db.add_release(
                mbid=rel["mbid"],
                artist_id=artist["id"],
                title=rel["title"],
                release_type=rel["type"],
                release_date=rel.get("date", ""),
                notified=1,
                source="soundcloud",
                soundcloud_track_id=rel.get("soundcloud_track_id", ""),
                artwork_url=rel.get("artwork_url", ""),
            )
            if inserted:
                count += 1

    return {"status": "added", "artist": artist, "releases_imported": count}


@app.delete("/api/artists/{artist_id}")
async def remove_artist(artist_id: int):
    db.remove_artist(artist_id)
    return {"status": "removed"}


@app.post("/api/artists/{artist_id}/unlink-itunes")
async def unlink_artist_itunes(artist_id: int):
    artist = db.unlink_artist_itunes(artist_id)
    return {"status": "unlinked", "artist": artist}


@app.post("/api/artists/{artist_id}/unlink-mb")
async def unlink_artist_mb(artist_id: int):
    artist = db.unlink_artist_mb(artist_id)
    return {"status": "unlinked", "artist": artist}


@app.post("/api/artists/{artist_id}/unlink-soundcloud")
async def unlink_artist_soundcloud(artist_id: int):
    artist = db.unlink_artist_soundcloud(artist_id)
    return {"status": "unlinked", "artist": artist}


class DisambiguationUpdate(BaseModel):
    disambiguation: str = ""


@app.post("/api/artists/{artist_id}/disambiguation")
async def set_artist_disambiguation(artist_id: int, body: DisambiguationUpdate):
    """Set a tracked artist's disambiguation note (free text, may be empty)."""
    artist = db.update_artist_disambiguation(artist_id, body.disambiguation or "")
    if artist is None:
        return {"status": "error", "message": "Artist not found"}
    return {"status": "ok", "artist": artist}


@app.get("/api/artists/export")
async def export_artists():
    """Export tracked artists as a JSON file."""
    import json
    from fastapi.responses import Response

    artists = db.get_all_artists()
    payload = {
        "version": 3,  # Bumped version due to soundcloud_permalink inclusion
        "artists": [
            {
                "mbid": a["mbid"],
                "name": a["name"],
                "disambiguation": a["disambiguation"],
                "itunes_artist_id": a.get("itunes_artist_id"),
                "soundcloud_permalink": a.get("soundcloud_permalink"),
            }
            for a in artists
        ]
    }
    return Response(
        content=json.dumps(payload, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="music-release-tracker-artists.json"'
        },
    )


@app.post("/api/artists/import")
async def import_artists(request: Request):
    """Import artists from a JSON file."""
    form = await request.form()
    file = form.get("file")
    if not file:
        return {"status": "error", "message": "No file provided"}

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"status": "error", "message": "Invalid JSON"}

    artists_data = data.get("artists", [])
    added = 0
    skipped = 0
    errors = []
    for entry in artists_data:
        mbid = entry.get("mbid")
        name = entry.get("name")
        disambiguation = entry.get("disambiguation", "")
        itunes_artist_id = entry.get("itunes_artist_id")
        soundcloud_permalink = entry.get("soundcloud_permalink")
        if itunes_artist_id:
            try:
                itunes_artist_id = int(itunes_artist_id)
            except (ValueError, TypeError):
                itunes_artist_id = None
        if not name:
            errors.append(f"Invalid entry (no name): {entry}")
            continue
        existing = None
        if mbid:
            existing = db.get_artist_by_mbid(mbid)
        if not existing and itunes_artist_id:
            existing = db.get_artist_by_itunes_id(itunes_artist_id)
        if not existing and soundcloud_permalink:
            existing = db.get_artist_by_soundcloud_permalink(soundcloud_permalink)
        if existing:
            skipped += 1
            continue
        try:
            db.add_artist(mbid or "", name, disambiguation, itunes_artist_id=itunes_artist_id, soundcloud_permalink=soundcloud_permalink)
            added += 1
        except Exception as e:
            errors.append(f"Failed to add {name}: {e}")

    return {"status": "ok", "added": added, "skipped": skipped, "errors": errors}


# --- Release endpoints ---
@app.get("/api/releases")
async def list_releases(
    artist_id: Optional[int] = Query(None),
    type: Optional[str] = Query(None),
    unseen_only: bool = Query(False),
    include_hidden: bool = Query(False),
    hidden_only: bool = Query(False),
):
    # Parse comma-separated type filter; empty means "all"
    type_list = [t.strip() for t in type.split(",")] if type else None
    return db.get_releases(
        artist_id=artist_id,
        release_type=type_list,
        unseen_only=unseen_only,
        include_hidden=include_hidden or hidden_only,
        hidden_only=hidden_only,
    )

@app.post("/api/releases/{release_id}/seen")
async def mark_seen(release_id: int):
    db.mark_release_seen(release_id)
    return {"status": "ok"}

@app.post("/api/releases/{release_id}/hide")
async def hide_release(release_id: int):
    db.set_release_visible(release_id, False)
    return {"status": "hidden"}

@app.post("/api/releases/{release_id}/unhide")
async def unhide_release(release_id: int):
    db.set_release_visible(release_id, True)
    return {"status": "visible"}

@app.post("/api/releases/all_seen")
async def mark_all_seen():
    db.mark_all_releases_seen()  # Custom function to update all releases
    return {"status": "ok"}
    
@app.get("/api/unseen_count")
async def get_unseen_count():
    count = db.get_unseen_count()
    return {"count": count}


@app.get("/api/releases/{release_id}/tracks")
async def get_release_tracks(release_id: str):
    """Fetch and return tracklist for a release.

    Routes to MusicBrainz or iTunes API based on the release source.
    """
    # Look up release to determine source and artist
    release = db.get_release_by_id(release_id)
    if not release:
        return {"mbid": release_id, "tracks": []}

    source = release.get("source", "musicbrainz")
    artist_id = release["artist_id"]

    # Fetch tracks from the appropriate API
    if source == "itunes":
        collection_id = release.get("itunes_collection_id") or release_id
        tracks = await itunes.get_release_tracks(int(collection_id))
    elif source == "soundcloud":
        track_id = release.get("soundcloud_track_id") or release_id
        tracks = await soundcloud.get_release_tracks(track_id)
    else:
        # Pass the tracked artist's MBID so their own credit is filtered out
        # of per-track "feat." listings
        tracks = await musicbrainz.get_release_tracks(release_id, main_mbid=release.get("artist_mbid") or "")

    # Find all single titles locally for this artist
    single_titles: set[str] = set()
    raw_titles = db.get_artist_single_titles(artist_id)
    single_titles = {t.strip().lower() for t in raw_titles}

    # Annotate each track with has_single flag
    result_tracks = []
    for track in tracks:
        title_lower = track["title"].strip().lower()
        has_single = False
        if single_titles:
            if title_lower in single_titles:
                has_single = True
            else:
                for st in single_titles:
                    if _titles_match(title_lower, st):
                        has_single = True
                        break
        result_tracks.append({**track, "has_single": has_single})

    return {"mbid": release_id, "tracks": result_tracks}


@app.get("/api/releases/{release_id}/streaming")
async def get_release_streaming(release_id: str):
    """Fetch and return external streaming URLs for a release.

    Streaming links only exist in MusicBrainz's url-rels, so iTunes-sourced
    releases simply return an empty list (the UI hides the section then).
    """
    release = db.get_release_by_id(release_id)
    if not release:
        return {"mbid": release_id, "streaming": []}

    source = release.get("source", "musicbrainz")
    if source == "itunes":
        return {"mbid": release_id, "streaming": []}
    if source == "soundcloud":
        return {"mbid": release_id, "streaming": []}

    streaming = await musicbrainz.get_release_streaming_urls(release_id)
    return {"mbid": release_id, "streaming": streaming}


def _titles_match(a: str, b: str) -> bool:
    """Heuristic title matcher ignoring common suffixes/prefixes."""
    # Strip common non-essential parts for comparison
    import re
    a_clean = re.sub(r'\s*\(.*?\)\s*', '', a).strip()
    b_clean = re.sub(r'\s*\(.*?\)\s*', '', b).strip()
    if a_clean == b_clean:
        return True
    # One contained within the other (longer title may have extra words)
    if len(a_clean) > len(b_clean):
        short, long_ = b_clean, a_clean
    else:
        short, long_ = a_clean, b_clean
    return short in long_ or long_ in short


# --- Check endpoint (SSE) ---

# Artists checked concurrently per scan. Request starts stay paced >=1s per
# source by the API rate limiters, so this only overlaps latency — it cannot
# hammer MusicBrainz/iTunes/SoundCloud.
SCAN_CONCURRENCY = 5


async def _check_one_artist(artist: dict) -> tuple[list[str], str]:
    """Check one artist. Returns (new_titles, "") or ([], error_message).

    Shared by the SSE scan and the startup refresh. A failure is logged
    here; callers decide whether to skip or retry.
    """
    artist_sources = _artist_sources(artist)
    sources_str = ", ".join(s.upper() for s in artist_sources)
    try:
        titles = await _fetch_artist_new_titles(artist)
        return titles, ""
    except Exception as e:
        error_msg = _error_message(e)
        _add_log("ERROR", f"Failed to fetch releases from {sources_str}, skipping for now",
                 artist=artist["name"], detail=error_msg)
        return [], f"{artist['name']} ({sources_str}): {error_msg}"

def _artist_sources(artist: dict) -> list[str]:
    """Sources configured for an artist, filtered by enabled settings."""
    enabled = _enabled_sources()
    sources = []
    if artist.get("mbid") and "musicbrainz" in enabled:
        sources.append("musicbrainz")
    if artist.get("itunes_artist_id") and "itunes" in enabled:
        sources.append("itunes")
    if artist.get("soundcloud_permalink") and "soundcloud" in enabled:
        sources.append("soundcloud")
    if sources:
        return sources
    # Artist has IDs but all of them are disabled -> fetch nothing
    if artist.get("mbid") or artist.get("itunes_artist_id") or artist.get("soundcloud_permalink"):
        return []
    # Legacy row with no IDs at all: keep old default so callers no-op safely
    return enabled[:1] if enabled else ["musicbrainz"]


async def _fetch_artist_new_titles(artist: dict) -> list[str]:
    """Fetch releases for one artist from every configured source.

    Returns the titles of newly inserted releases. Raises on API/network
    failure so callers can decide to skip/retry.
    """
    has_mb = bool(artist.get("mbid"))
    has_itunes = bool(artist.get("itunes_artist_id"))
    has_sc = bool(artist.get("soundcloud_permalink"))

    new_titles = []
    for source in _artist_sources(artist):
        if source == "musicbrainz" and has_mb:
            releases = await musicbrainz.get_artist_releases(artist["mbid"])
            for rel in releases:
                inserted = db.add_release(
                    mbid=rel["mbid"],
                    artist_id=artist["id"],
                    title=rel["title"],
                    release_type=rel["type"],
                    release_date=rel["date"],
                    notified=0,
                    source="musicbrainz",
                    mb_url=rel.get("url", ""),
                    credits=json.dumps(rel.get("credits") or []),
                )
                if inserted:
                    new_titles.append(rel["title"])
                    # Fetch and store tracklist for albums/EPs (used for SC dedup)
                    if rel["type"] in ("Album", "EP"):
                        try:
                            tracks = await musicbrainz.get_release_tracks(rel["mbid"], artist.get("mbid", ""))
                            if tracks:
                                release_row = db.get_release_by_mbid(rel["mbid"], artist["id"])
                                if release_row:
                                    db.update_release_track_titles(
                                        release_row["id"],
                                        [t["title"] for t in tracks],
                                    )
                        except Exception:
                            pass
        elif source == "itunes" and has_itunes:
            releases = await itunes.get_artist_releases(artist["itunes_artist_id"], artist_name=artist["name"])
            # Get existing MB releases for this artist to check for duplicates
            existing_releases = db.get_releases(artist_id=artist["id"])
            mb_titles = {_normalize_release_title(r["title"]) for r in existing_releases if r.get("source") == "musicbrainz"}
            for rel in releases:
                # Skip iTunes releases that duplicate MB releases (ignoring - Single/- EP suffix)
                itunes_title_norm = _normalize_release_title(rel["title"])
                if itunes_title_norm in mb_titles:
                    continue
                inserted = db.add_release(
                    mbid=str(rel["id"]),
                    artist_id=artist["id"],
                    title=rel["title"],
                    release_type=rel["type"],
                    release_date=rel.get("date", ""),
                    notified=0,
                    source="itunes",
                    itunes_collection_id=str(rel["id"]),
                    artwork_url=rel.get("artwork_url", ""),
                )
                if inserted:
                    new_titles.append(rel["title"])
        elif source == "soundcloud" and has_sc:
            releases = await soundcloud.get_artist_releases(artist["soundcloud_permalink"])
            # Deduplicate: check against existing release titles AND album/EP tracklists.
            existing_releases = db.get_releases(artist_id=artist["id"])
            existing_titles = {_normalize_release_title(r["title"]) for r in existing_releases}
            # Also collect track titles from album/EP tracklists
            album_track_titles = db.get_artist_all_track_titles(artist["id"])
            for rel in releases:
                sc_title_norm = _normalize_release_title(rel["title"])
                if sc_title_norm in existing_titles:
                    continue
                if sc_title_norm in album_track_titles:
                    continue
                inserted = db.add_release(
                    mbid=rel["mbid"],
                    artist_id=artist["id"],
                    title=rel["title"],
                    release_type=rel["type"],
                    release_date=rel.get("date", ""),
                    notified=0,
                    source="soundcloud",
                    soundcloud_track_id=rel.get("soundcloud_track_id", ""),
                    artwork_url=rel.get("artwork_url", ""),
                )
                if inserted:
                    new_titles.append(rel["title"])

    return new_titles


def _error_message(e: Exception) -> str:
    msg = str(e) if str(e) else repr(e)
    response = getattr(e, "response", None)
    if response is not None:
        msg = f"HTTP {response.status_code}: {msg}"
    elif isinstance(e, httpx.TimeoutException):
        msg = f"Timeout after retries: {msg}" if msg else "Read timeout after retries"
    return msg


@app.get("/api/check")
async def check_releases(skip: int = Query(0), artist_id: Optional[int] = Query(None)):
    async def event_stream():
        artists = db.get_all_artists()

        # Filter to specific artist if artist_id is provided
        if artist_id is not None:
            artists = [a for a in artists if a["id"] == artist_id]
            if not artists:
                yield _sse({"type": "done", "message": "Artist not found.", "summary": []})
                return

        total = len(artists)

        _add_log("INFO", f"Starting scan for {total} artist(s) (skip={skip}) — enabled sources: {', '.join(s.upper() for s in _enabled_sources())}")

        if total == 0 or skip >= total:
            yield _sse({"type": "done", "message": "No artists to check.", "summary": [], "skip": skip, "total_checked": 0})
            return

        summary: list[dict] = []
        failed_artists: list[dict] = []

        def record_success(artist: dict, new_titles: list[str]):
            _add_log("INFO", f"Checked OK - {len(new_titles)} new release(s)", artist=artist["name"])
            if new_titles:
                summary.append({
                    "artist": artist["name"],
                    "new_releases": new_titles,
                })

        # --- Pass 1: everyone, up to SCAN_CONCURRENCY artists at once ---
        # Completions arrive out of order. `completed` counts finished artists
        # (drives the progress bar); `resume_at` is the smallest unchecked
        # 1-based index, so a pause/resume reconnect never skips anyone
        # (re-checks are idempotent).
        sem = asyncio.Semaphore(SCAN_CONCURRENCY)
        pending = artists[skip:]
        base = skip  # 0-based offset of pending[0] in artists
        done_idx: set[int] = set()

        async def run_one(offset: int, artist: dict) -> tuple[int, list[str], str]:
            async with sem:
                new_titles, error_msg = await _check_one_artist(artist)
                return offset, new_titles, error_msg

        tasks = [asyncio.create_task(run_one(n, a)) for n, a in enumerate(pending)]
        try:
            for coro in asyncio.as_completed(tasks):
                offset, new_titles, error_msg = await coro
                artist = pending[offset]
                done_idx.add(offset)
                completed = len(done_idx)
                resume_at = next(
                    (n for n in range(len(pending)) if n not in done_idx),
                    len(pending),
                )
                if error_msg:
                    failed_artists.append(artist)
                    # Non-fatal: tell the UI about it but keep scanning
                    yield _sse({"type": "warning", "message": f"Skipped {error_msg}"})
                else:
                    record_success(artist, new_titles)
                yield _sse({
                    "type": "progress",
                    "message": f"artist {completed} of {total}: {artist['name']} checked",
                    "current": base + resume_at,
                    "completed": completed,
                    "resume": base + resume_at,
                    "total": total,
                })
        finally:
            # Pause/close must not leave orphaned checks hammering the APIs
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        # --- Pass 2: one retry for artists that failed (transient timeouts / 503s) ---
        if failed_artists:
            retry_count = len(failed_artists)
            _add_log("INFO", f"Retrying {retry_count} failed artist(s)...")
            yield _sse({
                "type": "progress",
                "message": f"Retrying {retry_count} failed artist(s)...",
                "current": total,
                "completed": total,
                "resume": total,
                "total": total,
            })
            for artist in list(failed_artists):
                new_titles, error_msg = await _check_one_artist(artist)
                if error_msg:
                    yield _sse({"type": "warning", "message": f"Skipped again {error_msg}"})
                    continue  # stays in failed_artists
                failed_artists.remove(artist)
                record_success(artist, new_titles)

        failed_names = [a["name"] for a in failed_artists]
        total_new = sum(len(s["new_releases"]) for s in summary)
        done_msg = f"Done! Found {total_new} new release(s)."
        if failed_names:
            done_msg += f" {len(failed_names)} artist(s) could not be checked - see Logs."
            _add_log("WARNING", f"Scan finished with {len(failed_names)} unchecked artist(s)",
                     detail=", ".join(failed_names))
        else:
            _add_log("INFO", f"Scan complete. Found {total_new} new release(s).")

        yield _sse({
            "type": "done",
            "message": done_msg,
            "summary": summary,
            "failed": failed_names,
        })

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/logs")
async def get_logs(limit: int = Query(100)):
    """Return recent scan log entries."""
    return logstore.get_logs(limit)


@app.post("/api/logs/clear")
async def clear_logs():
    """Clear the in-memory log store."""
    logstore.clear_logs()
    return {"status": "cleared"}


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# --- Settings endpoints ---

@app.get("/api/settings")
async def get_settings():
    """Return all settings."""
    return db.get_all_settings()


@app.post("/api/settings")
async def save_settings(request: Request):
    """Save settings."""
    body = await request.json()
    for key, value in body.items():
        # Dedup lists have dedicated endpoints (JSON); skip here to avoid
        # storing a Python-repr string accidentally.
        if key in ("dedup_ignores", "explicit_map"):
            continue
        db.set_setting(key, str(value))
    return {"status": "ok"}


@app.get("/api/settings/dedup-ignores")
async def get_dedup_ignores():
    """Return user-defined title suffixes ignored for duplicate detection."""
    return {"ignores": db.get_dedup_ignores(), "defaults": db.DEFAULT_DEDUP_IGNORES}


@app.put("/api/settings/dedup-ignores")
async def put_dedup_ignores(request: Request):
    """Replace the title ignore list (validated, deduped)."""
    body = await request.json()
    ignores = body.get("ignores", [])
    if not isinstance(ignores, list):
        return {"status": "error", "message": "ignores must be a list"}
    cleaned = db.set_dedup_ignores(ignores)
    return {"status": "ok", "ignores": cleaned}


@app.get("/api/settings/explicit-map")
async def get_explicit_map():
    """Return user-defined [censored, clean] pairs for duplicate detection."""
    return {"mappings": db.get_explicit_map(), "defaults": db.DEFAULT_EXPLICIT_MAP}


@app.put("/api/settings/explicit-map")
async def put_explicit_map(request: Request):
    """Replace the explicit-word mapping list (validated, deduped)."""
    body = await request.json()
    mappings = body.get("mappings", [])
    if not isinstance(mappings, list):
        return {"status": "error", "message": "mappings must be a list"}
    cleaned = db.set_explicit_map(mappings)
    return {"status": "ok", "mappings": cleaned}


@app.post("/api/dedup")
async def run_dedup():
    """Run deduplication manually on all existing releases."""
    hidden = _remove_startup_duplicates()
    return {"hidden": hidden}


# --- Startup dedup ---

def _remove_startup_duplicates() -> int:
    """Remove duplicate releases from the database.

    Finds releases with matching (artist_id, normalized title) and keeps only
    the one with the earliest first_seen_at. Returns the number removed.
    """
    def _norm(title: str) -> str:
        return db.normalize_release_title(title)

    conn = db.get_db()
    hidden = 0
    rows = conn.execute(
        "SELECT id, artist_id, title FROM releases ORDER BY artist_id, first_seen_at"
    ).fetchall()

    # Group by (artist_id, normalized_title)
    groups: dict[tuple[int, str], list[int]] = {}
    for row in rows:
        key = (row["artist_id"], _norm(row["title"]))
        groups.setdefault(key, []).append(row["id"])

    for ids in groups.values():
        if len(ids) > 1:
            # Keep the first (earliest first_seen_at), hide the rest
            to_hide = ids[1:]
            placeholders = ",".join("?" for _ in to_hide)
            conn.execute(f"UPDATE releases SET is_visible = 0 WHERE id IN ({placeholders})", to_hide)
            hidden += len(to_hide)

    # Also hide SC releases whose titles appear in album/EP tracklists
    import json
    track_rows = conn.execute(
        "SELECT artist_id, track_titles FROM releases WHERE track_titles != ''"
    ).fetchall()
    # Build set of (artist_id, normalized_track_title) from album/EP tracklists
    album_tracks: set[tuple[int, str]] = set()
    for tr in track_rows:
        try:
            tracks = json.loads(tr["track_titles"])
            for t in tracks:
                album_tracks.add((tr["artist_id"], _norm(t)))
        except (json.JSONDecodeError, TypeError):
            continue

    if album_tracks:
        sc_rows = conn.execute(
            "SELECT id, artist_id, title FROM releases WHERE source = 'soundcloud' AND is_visible = 1"
        ).fetchall()
        to_hide = []
        for sr in sc_rows:
            if (sr["artist_id"], _norm(sr["title"])) in album_tracks:
                to_hide.append(sr["id"])
        if to_hide:
            placeholders = ",".join("?" for _ in to_hide)
            conn.execute(f"UPDATE releases SET is_visible = 0 WHERE id IN ({placeholders})", to_hide)
            hidden += len(to_hide)

    conn.commit()
    return hidden


async def _backfill_tracklists():
    """Fetch and store tracklists for MB album/EP releases that are missing them."""
    import asyncio
    conn = db.get_db()
    rows = conn.execute(
        """SELECT r.id, r.mbid, r.artist_id, a.mbid as artist_mbid
           FROM releases r
           JOIN artists a ON r.artist_id = a.id
           WHERE r.source = 'musicbrainz'
           AND r.release_type IN ('Album', 'EP')
           AND (r.track_titles = '' OR r.track_titles IS NULL)"""
    ).fetchall()

    if not rows:
        return

    _add_log("INFO", f"Backfilling tracklists for {len(rows)} MB release(s)...")
    # Bounded concurrency: request starts stay paced >=1s by the MB limiter,
    # but in-flight requests overlap instead of running strictly serially.
    sem = asyncio.Semaphore(4)
    filled = 0

    async def _fill_one(row) -> bool:
        async with sem:
            try:
                tracks = await musicbrainz.get_release_tracks(row["mbid"], row["artist_mbid"] or "")
            except Exception:
                return False
            if tracks:
                db.update_release_track_titles(row["id"], [t["title"] for t in tracks])
                return True
            return False

    for coro in asyncio.as_completed([_fill_one(r) for r in rows]):
        try:
            if await coro:
                filled += 1
        except Exception:
            continue
    if filled:
        _add_log("INFO", f"Backfilled tracklists for {filled} release(s)")


# --- Source settings helper ---

def _enabled_sources() -> list[str]:
    """Return list of enabled source keys from settings."""
    settings = db.get_all_settings()
    sources = []
    if settings.get("source_musicbrainz", "1") == "1":
        sources.append("musicbrainz")
    if settings.get("source_itunes", "1") == "1":
        sources.append("itunes")
    if settings.get("source_soundcloud", "1") == "1":
        sources.append("soundcloud")
    return sources


# --- Entry point ---

if __name__ == "__main__":
    import uvicorn

    print(f"\n  Music Release Tracker")
    print(f"  Running at http://{HOST}:{PORT}\n")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
