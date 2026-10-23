from __future__ import annotations

import asyncio
import json
import logging
import webbrowser
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

import httpx
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import db
import musicbrainz
import itunes
import soundcloud
import logstore
import covers
import cache

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
    # Take a portable artist snapshot before init_db() runs any migrations.
    # If a future schema change goes wrong, the artist list is still exportable
    # from BACKUP/ without needing the database.
    artists_backup_path = db.backup_artists_json()
    db.init_db()
    backup_path = db.backup_database()
    if backup_path:
        _add_log("INFO", f"Database backup saved to {backup_path}")
    if not artists_backup_path:
        artists_backup_path = db.backup_artists_json()
    if artists_backup_path:
        _add_log("INFO", f"Artist list snapshot saved to {artists_backup_path}")
    try:
        counts = db.get_counts()
        _add_log("INFO", f"Using database {db.DB_PATH} "
                 f"({counts['artists']} artists, {counts['releases']} releases)")
    except Exception as e:
        _add_log("ERROR", "Could not read database stats", detail=str(e))
    # Run startup dedup if enabled, and once when the dedup policy changes so
    # an existing library is repaired without forcing a check on every boot.
    settings = db.get_all_settings()
    policy_stale = db.get_meta("dedup_policy_version") != str(DEDUP_POLICY_VERSION)
    if settings.get("startup_dedup") == "1" or policy_stale:
        if policy_stale:
            _add_log("INFO", "Applying updated duplicate rules...")
        else:
            _add_log("INFO", "Running startup duplicate check...")
        result = await _dedupe_releases()
        if policy_stale:
            db.set_meta("dedup_policy_version", str(DEDUP_POLICY_VERSION))
        if result["hidden"] or result["restored"]:
            _add_log("INFO", f"Hid {result['hidden']} duplicate release(s), "
                             f"restored {result['restored']}; "
                             f"{result['total_hidden']} total hidden")
        else:
            _add_log("INFO", f"No new duplicates found; {result['total_hidden']} total hidden")
    # Check all artists for new releases if enabled (background, non-blocking)
    if settings.get("startup_refresh") == "1":
        asyncio.create_task(_startup_refresh())
    # Retroactively fetch tracklists for MB albums/EPs missing track_titles
    asyncio.create_task(_backfill_tracklists())
    # Retroactively fetch tracklists for SoundCloud albums/EPs too
    asyncio.create_task(_backfill_sc_tracklists())
    # Retroactively fetch canonical URLs for SoundCloud releases missing one
    asyncio.create_task(_backfill_sc_urls())
    # Refresh the artist JSON snapshot while the app stays open.
    artist_backup_task = asyncio.create_task(_periodic_artist_backup())
    webbrowser.open(f"http://{HOST}:{PORT}")
    yield
    artist_backup_task.cancel()
    try:
        await artist_backup_task
    except asyncio.CancelledError:
        pass


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
    # Final pass applies manual hide/unhide changes even when this refresh
    # inserted no new rows.
    await _dedupe_releases()
    _add_log("INFO", f"Startup refresh complete. Found {total_new} new release(s).")


# The database snapshot is only written at startup; keep a portable artist
# list snapshot fresh while the app runs for days at a time.
ARTIST_BACKUP_INTERVAL_SECONDS = 24 * 60 * 60


async def _periodic_artist_backup():
    """Write an artist JSON snapshot to BACKUP/ once a day."""
    while True:
        await asyncio.sleep(ARTIST_BACKUP_INTERVAL_SECONDS)
        try:
            path = db.backup_artists_json()
        except Exception as e:
            _add_log("WARNING", "Could not save artist list snapshot",
                     detail=_error_message(e))
            continue
        if path:
            _add_log("INFO", f"Artist list snapshot saved to {path}")


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
    artist_id: int | None = None  # explicit link target (link-source modal)
    force: bool = False  # confirmed "replace + unlink from old owner" retry

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

        # MB-first: when the top MB hit exactly matches the query, its URL
        # relationships may already confirm the Apple Music ID / SoundCloud
        # profile — ground truth that makes the fuzzy iTunes text search and
        # SC slug probing redundant. Skipping those saves several API calls
        # (SC probing fans out to many oEmbed/v2 requests). Otherwise every
        # selected platform is queried in parallel as before.
        mb_results: list = []
        it_results: list = []
        sc_results: list = []
        mb_confirmed: dict | None = None
        if "musicbrainz" in use:
            mb_results = await _search_mb()
            if mb_results:
                top = mb_results[0]
                if top.get("mbid") and _normalize_name(top.get("name", "")) == _normalize_name(query):
                    try:
                        links = await cache.cached(
                            f"mb_links:{top['mbid']}",
                            cache.TTL_MB_LINKS,
                            lambda: musicbrainz.get_artist_links(top["mbid"]),
                        )
                    except Exception:
                        links = None
                    if links and (links.get("itunes_artist_id") or links.get("soundcloud_permalink")):
                        mb_confirmed = {
                            "mbid": top["mbid"],
                            "name": top.get("name", ""),
                            "itunes_artist_id": links.get("itunes_artist_id"),
                            "soundcloud_permalink": links.get("soundcloud_permalink"),
                            "streaming": links.get("streaming") or [],
                        }

        coros = {}
        # Skip a platform search when MB already confirmed that platform's link
        if "itunes" in use and not (mb_confirmed and mb_confirmed.get("itunes_artist_id")):
            coros["itunes"] = _search_itunes()
        if "soundcloud" in use and not (mb_confirmed and mb_confirmed.get("soundcloud_permalink")):
            coros["soundcloud"] = _search_sc()
        if coros:
            gathered = await asyncio.gather(*coros.values())
            by_source = dict(zip(coros.keys(), gathered))
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

        # MB-confirmed IDs (from the skipped platform searches above) take
        # precedence over anything name-merged and are flagged so the UI can
        # show them as verified without refetching.
        if mb_confirmed:
            entry = merged.get(_normalize_name(mb_confirmed["name"]))
            if entry is not None and entry.get("mbid") == mb_confirmed["mbid"]:
                if mb_confirmed.get("itunes_artist_id"):
                    entry["itunes_artist_id"] = mb_confirmed["itunes_artist_id"]
                    entry["_confirmed_apple"] = True
                    if entry.get("source") == "musicbrainz":
                        entry["source"] = "both"
                if mb_confirmed.get("soundcloud_permalink"):
                    entry["soundcloud_permalink"] = mb_confirmed["soundcloud_permalink"]
                    entry["_confirmed_sc"] = True
                    if entry.get("source") in ("musicbrainz", "itunes", "both"):
                        entry["source"] += "+sc"
                entry["_mb_streaming"] = mb_confirmed.get("streaming") or []
                entry["_mb_enriched"] = True

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

    # Explicit link from the link-source modal (currently SoundCloud only):
    # the user picked which tracked row to link onto, so the SoundCloud
    # display name ("Avicii Official") must never spawn a new artist entry
    # when it differs from the tracked name ("Avicii").
    if body.source == "soundcloud" and body.artist_id is not None:
        target = db.get_artist_by_id(body.artist_id)
        if target is None:
            return {"status": "error", "message": "Artist not found"}
        permalink = (body.id or "").strip()
        if not permalink:
            return {"status": "error", "message": "Missing SoundCloud permalink"}

        owner = db.get_artist_by_soundcloud_permalink(permalink)
        if owner and owner["id"] == target["id"]:
            return {"status": "already_exists", "artist": target}
        if owner and owner["id"] != target["id"] and not body.force:
            return {
                "status": "conflict",
                "message": f"already linked to {owner.get('name', '')}",
                "conflicting_artist": {"id": owner["id"], "name": owner.get("name", "")},
                "artist": target,
            }
        if owner and owner["id"] != target["id"] and body.force:
            # Confirmed replace: unlink from the old owner first (also
            # deletes the old owner's SC releases, same as manual unlink).
            db.unlink_artist_soundcloud(owner["id"])
            target = db.get_artist_by_id(body.artist_id)
            if target is None:
                return {"status": "error", "message": "Artist not found"}

        # Target already points at a different profile: drop its old SC
        # releases so two profiles' tracks don't mix under one row.
        current = (target.get("soundcloud_permalink") or "").strip()
        if current and current != permalink:
            conn = db.get_db()
            conn.execute(
                "DELETE FROM releases WHERE artist_id = ? AND source = 'soundcloud'",
                (target["id"],),
            )
            conn.commit()

        artist = db.link_artist(
            artist_id=target["id"],
            mbid=target.get("mbid", ""),
            itunes_artist_id=target.get("itunes_artist_id"),
            soundcloud_permalink=permalink,
        )

        count = 0
        releases = await soundcloud.get_artist_releases(permalink)
        async with db.batch():
            for rel in releases:
                inserted = db.add_release(
                    mbid=rel["mbid"],
                    artist_id=artist["id"],
                    title=rel["title"],
                    release_type=rel["type"],
                    release_date=rel.get("date", ""),
                    notified=1,
                    source="soundcloud",
                    soundcloud_track_id=rel.get("soundcloud_track_id", ""),
                    soundcloud_playlist_id=rel.get("soundcloud_playlist_id") or None,
                    artwork_url=rel.get("artwork_url", ""),
                    mb_url=rel.get("url", ""),
                    track_titles=rel.get("track_titles") or [],
                )
                if inserted:
                    count += 1

        await _dedupe_releases()
        return {"status": "linked", "artist": artist, "releases_imported": count}

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
        # Guard: the incoming source ID may already be owned by a DIFFERENT
        # tracked artist (e.g. an MB-confirmed SC permalink that belongs to
        # another name variant). Stealing it would corrupt both rows, so
        # return the same conflict shape the link-source modal uses and let
        # the UI show the existing "already taken" prompt.
        owner = None
        try:
            if body.source == "musicbrainz" and body.id:
                owner = db.get_artist_by_mbid(body.id)
            elif body.source == "itunes" and body.id:
                owner = db.get_artist_by_itunes_id(int(body.id))
            elif body.source == "soundcloud" and body.id:
                owner = db.get_artist_by_soundcloud_permalink(body.id)
        except (ValueError, TypeError):
            owner = None
        if owner and owner["id"] != existing_by_name["id"]:
            return {
                "status": "conflict",
                "message": f"already linked to {owner.get('name', '')}",
                "conflicting_artist": {"id": owner["id"], "name": owner.get("name", "")},
                "artist": existing_by_name,
            }
        # Link the new source ID to existing artist
        artist = db.link_artist(
            artist_id=existing_by_name["id"],
            mbid=body.id if body.source == "musicbrainz" else existing_by_name.get("mbid", ""),
            itunes_artist_id=int(body.id) if body.source == "itunes" else existing_by_name.get("itunes_artist_id"),
            soundcloud_permalink=body.id if body.source == "soundcloud" else existing_by_name.get("soundcloud_permalink"),
        )

        # Import releases for the linked source
        count = 0
        async with db.batch():
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
                # MB is authoritative: hide iTunes/SoundCloud copies that
                # share a title and release type with an MB entry.
                db.hide_lower_priority_title_duplicates(artist["id"], "musicbrainz")
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
                # iTunes outranks SoundCloud: hide SC copies of these titles.
                db.hide_lower_priority_title_duplicates(artist["id"], "itunes")
            elif body.source == "soundcloud":
                releases = await soundcloud.get_artist_releases(body.id)
                # Store every fetched track/playlist; dedup runs after import.
                for rel in releases:
                    inserted = db.add_release(
                        mbid=rel["mbid"],
                        artist_id=artist["id"],
                        title=rel["title"],
                        release_type=rel["type"],
                        release_date=rel.get("date", ""),
                        notified=1,
                        source="soundcloud",
                        soundcloud_track_id=rel.get("soundcloud_track_id", ""),
                        soundcloud_playlist_id=rel.get("soundcloud_playlist_id") or None,
                        artwork_url=rel.get("artwork_url", ""),
                        mb_url=rel.get("url", ""),
                        track_titles=rel.get("track_titles") or [],
                    )
                    if inserted:
                        count += 1

        await _dedupe_releases()
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
    async with db.batch():
        if body.source == "musicbrainz":
            releases = await musicbrainz.get_artist_releases(body.id)
            # (release row id, release-group mbid) for new albums/EPs —
            # tracklists fetched in parallel after the loop instead of
            # one awaited request per album.
            pending_tracklists: list[tuple[int, str]] = []
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
                        release_row = db.get_release_by_mbid(rel["mbid"], artist["id"])
                        if release_row:
                            pending_tracklists.append((release_row["id"], rel["mbid"]))
            await _fetch_tracklists_parallel(pending_tracklists, body.id)
            db.hide_lower_priority_title_duplicates(artist["id"], "musicbrainz")
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
            db.hide_lower_priority_title_duplicates(artist["id"], "itunes")
        elif body.source == "soundcloud":
            releases = await soundcloud.get_artist_releases(body.id)
            # Store every fetched track/playlist; dedup runs after import.
            for rel in releases:
                inserted = db.add_release(
                    mbid=rel["mbid"],
                    artist_id=artist["id"],
                    title=rel["title"],
                    release_type=rel["type"],
                    release_date=rel.get("date", ""),
                    notified=1,
                    source="soundcloud",
                    soundcloud_track_id=rel.get("soundcloud_track_id", ""),
                    soundcloud_playlist_id=rel.get("soundcloud_playlist_id") or None,
                    artwork_url=rel.get("artwork_url", ""),
                    mb_url=rel.get("url", ""),
                    track_titles=rel.get("track_titles") or [],
                )
                if inserted:
                    count += 1

    await _dedupe_releases()
    return {"status": "added", "artist": artist, "releases_imported": count}


@app.delete("/api/artists/{artist_id}")
async def remove_artist(artist_id: int):
    db.remove_artist(artist_id)
    return {"status": "removed"}


@app.post("/api/artists/{artist_id}/unlink-itunes")
async def unlink_artist_itunes(artist_id: int):
    artist = db.unlink_artist_itunes(artist_id)
    # A hidden complementary copy may now be the only version left.
    await _dedupe_releases()
    return {"status": "unlinked", "artist": artist}


@app.post("/api/artists/{artist_id}/unlink-mb")
async def unlink_artist_mb(artist_id: int):
    artist = db.unlink_artist_mb(artist_id)
    # iTunes/SoundCloud copies hidden in favour of MB must fill back in.
    await _dedupe_releases()
    return {"status": "unlinked", "artist": artist}


@app.post("/api/artists/{artist_id}/unlink-soundcloud")
async def unlink_artist_soundcloud(artist_id: int):
    artist = db.unlink_artist_soundcloud(artist_id)
    await _dedupe_releases()
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
    from fastapi.responses import Response

    payload = db.artists_export_payload()
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

# Feed paging. The client asks for one page at a time and follows the opaque
# X-Next-Cursor header, so the feed never ships the whole library at once.
FEED_PAGE_SIZE = 60
FEED_PAGE_MAX = 200

# Cursor layout: "release_date|first_seen_at|id". Neither date string can
# contain the separator, so a plain split is safe.
_CURSOR_SEP = "|"


def _decode_cursor(raw: str) -> tuple[str, str, int] | None:
    """Parse an X-Next-Cursor value, or None when absent/malformed."""
    if not raw:
        return None
    parts = raw.split(_CURSOR_SEP)
    if len(parts) != 3:
        return None
    date, time, raw_id = parts
    try:
        return date, time, int(raw_id)
    except (TypeError, ValueError):
        return None


def _encode_cursor(row: dict) -> str:
    return f"{row['release_date']}{_CURSOR_SEP}{row['first_seen_at']}{_CURSOR_SEP}{row['id']}"


@app.get("/api/releases")
async def list_releases(
    response: Response,
    artist_id: Optional[int] = Query(None),
    type: Optional[str] = Query(None),
    unseen_only: bool = Query(False),
    include_hidden: bool = Query(False),
    hidden_only: bool = Query(False),
    limit: int = Query(FEED_PAGE_SIZE, ge=1, le=FEED_PAGE_MAX),
    cursor: str = Query(""),
):
    # Parse comma-separated type filter; empty means "all"
    type_list = [t.strip() for t in type.split(",")] if type else None
    filters = dict(
        artist_id=artist_id,
        release_type=type_list,
        unseen_only=unseen_only,
        include_hidden=include_hidden or hidden_only,
        hidden_only=hidden_only,
    )
    releases = db.get_releases(**filters, limit=limit, cursor=_decode_cursor(cursor))
    # X-Total-Count lets the UI show "N of M"; X-Next-Cursor (empty on the
    # last page) tells it whether to keep paging.
    response.headers["X-Total-Count"] = str(db.count_releases(**filters))
    if len(releases) == limit:
        response.headers["X-Next-Cursor"] = _encode_cursor(releases[-1])
    else:
        response.headers["X-Next-Cursor"] = ""
    return releases

@app.get("/api/covers/{mbid}")
async def get_release_cover(mbid: str):
    """Serve a Cover Art Archive image, cached on disk.

    Returns 404 when the release group has no cover (also cached, so the
    browser's onerror fallback doesn't trigger a fresh upstream request on
    every feed load).
    """
    result = await covers.get_cover(mbid)
    if result is None:
        return Response(
            status_code=404,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    data, content_type = result
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=604800"},
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


def _upstream_error_status(e: Exception) -> int:
    """Map an upstream fetch failure to a downstream HTTP status.

    503/429 (throttling) -> 503, timeouts -> 504, everything else -> 502.
    Used by on-demand endpoints so a MusicBrainz outage degrades to an
    empty payload instead of an unhandled ASGI exception (HTTP 500).
    """
    response = getattr(e, "response", None)
    status = getattr(response, "status_code", None)
    if status in (503, 429):
        return 503
    if isinstance(e, httpx.TimeoutException):
        return 504
    if isinstance(e, (httpx.HTTPStatusError, httpx.RequestError)):
        return 502
    return 502


@app.get("/api/releases/{release_id}/tracks")
async def get_release_tracks(release_id: str):
    """Fetch and return tracklist for a release.

    Routes to MusicBrainz or iTunes API based on the release source.
    On upstream failure (e.g. MusicBrainz 503) returns an empty tracklist
    with an `error` message and a 502/503/504 status instead of raising.
    """
    # Look up release to determine source and artist
    release = db.get_release_by_id(release_id)
    if not release:
        return {"mbid": release_id, "tracks": []}

    source = release.get("source", "musicbrainz")
    artist_id = release["artist_id"]

    # Fetch tracks from the appropriate API. Cached per release row so repeat
    # expands (this session or after a restart) are a local read; the failure
    # handling below still sees the original upstream exception.
    async def _fetch_tracks() -> list[dict]:
        if source == "itunes":
            collection_id = release.get("itunes_collection_id") or release_id
            return await itunes.get_release_tracks(int(collection_id))
        if source == "soundcloud":
            playlist_id = release.get("soundcloud_playlist_id")
            if playlist_id:
                return await soundcloud.get_playlist_tracks(playlist_id)
            track_id = release.get("soundcloud_track_id") or release_id
            return await soundcloud.get_release_tracks(track_id)
        # Pass the tracked artist's MBID so their own credit is filtered out
        # of per-track "feat." listings
        return await musicbrainz.get_release_tracks(
            release_id, main_mbid=release.get("artist_mbid") or ""
        )

    try:
        tracks = await cache.cached(
            f"tracks:{release['id']}", cache.TTL_TRACKS, _fetch_tracks
        )
    except Exception as e:
        msg = _error_message(e)
        logger.warning(f"Tracklist fetch failed for {release_id}: {msg}")
        return JSONResponse(
            status_code=_upstream_error_status(e),
            content={"mbid": release_id, "tracks": [], "error": msg},
        )

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
    On upstream failure (e.g. MusicBrainz 503) returns an empty list with
    an `error` message and a 502/503/504 status instead of raising.
    """
    release = db.get_release_by_id(release_id)
    if not release:
        return {"mbid": release_id, "streaming": []}

    source = release.get("source", "musicbrainz")
    if source == "itunes":
        return {"mbid": release_id, "streaming": []}
    if source == "soundcloud":
        return {"mbid": release_id, "streaming": []}

    try:
        streaming = await cache.cached(
            f"streaming:{release_id}",
            cache.TTL_STREAMING,
            lambda: musicbrainz.get_release_streaming_urls(release_id),
        )
    except Exception as e:
        msg = _error_message(e)
        logger.warning(f"Streaming fetch failed for {release_id}: {msg}")
        return JSONResponse(
            status_code=_upstream_error_status(e),
            content={"mbid": release_id, "streaming": [], "error": msg},
        )
    return {"mbid": release_id, "streaming": streaming}


@app.get("/api/mb-artists/{mbid}/links")
async def get_mb_artist_links(mbid: str):
    """Return an MB artist's streaming links plus confirmed platform IDs.

    Single MusicBrainz lookup (inc=url-rels), parsed once for two purposes:
      - streaming: full streaming-service link list for on-demand display.
      - itunes_artist_id / soundcloud_permalink: ground truth from the
        artist's official URL relationships. The frontend prefers these
        over fuzzy name-based merges when rendering search badges.

    Returns {"mbid": ..., "streaming": [...],
             "itunes_artist_id": int|None,
             "soundcloud_permalink": str|None}.
    On upstream failure (e.g. MusicBrainz 503 after retries) returns empty
    links with an `error` message and a 502/503/504 status instead of
    raising (which previously surfaced as an ASGI exception / HTTP 500).
    """
    import re as _re
    if not _re.match(
        r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
        r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', mbid or ""
    ):
        return {"mbid": mbid, "streaming": [],
                "itunes_artist_id": None, "soundcloud_permalink": None}
    try:
        links = await cache.cached(
            f"mb_links:{mbid}",
            cache.TTL_MB_LINKS,
            lambda: musicbrainz.get_artist_links(mbid),
        )
    except Exception as e:
        msg = _error_message(e)
        logger.warning(f"Artist links fetch failed for {mbid}: {msg}")
        return JSONResponse(
            status_code=_upstream_error_status(e),
            content={"mbid": mbid, "streaming": [],
                     "itunes_artist_id": None, "soundcloud_permalink": None,
                     "error": msg},
        )
    return {"mbid": mbid, **links}


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

    Returns the titles of newly inserted releases that are still visible
    after dedup. Raises on API/network failure so callers can decide to
    skip/retry.

    All of one artist's writes land in a single transaction (batch()), so a
    mid-artist failure leaves no partial state behind. Every fetched release
    is stored; dedup runs afterwards to hide duplicate copies.
    """
    has_mb = bool(artist.get("mbid"))
    has_itunes = bool(artist.get("itunes_artist_id"))
    has_sc = bool(artist.get("soundcloud_permalink"))

    new_titles = []
    # Rows inserted/refreshed this pass, so the scan summary can drop copies
    # that dedup hid immediately afterwards.
    inserted_rows: list[tuple[int, str]] = []
    # (release row id, release-group mbid) for new albums/EPs whose
    # tracklists still need fetching — collected in the MusicBrainz branch,
    # fetched in parallel right after it so the dedup pass that follows the
    # import sees the tracklists too.
    pending_tracklists: list[tuple[int, str]] = []
    async with db.batch():
        mb_changed = False
        itunes_changed = False
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
                        return_id=True,
                    )
                    if inserted:
                        new_titles.append(rel["title"])
                        if isinstance(inserted, int) and not isinstance(inserted, bool):
                            inserted_rows.append((inserted, rel["title"]))
                        mb_changed = True
                        # Fetch and store tracklist for albums/EPs (used for SC dedup)
                        if rel["type"] in ("Album", "EP"):
                            release_row = db.get_release_by_mbid(rel["mbid"], artist["id"])
                            if release_row:
                                pending_tracklists.append((release_row["id"], rel["mbid"]))
                # Fetch this pass's tracklists before the dedup pass that
                # follows the import, so SoundCloud track duplicates are
                # matched against them.
                if pending_tracklists:
                    await _fetch_tracklists_parallel(pending_tracklists, artist.get("mbid", ""))
                    pending_tracklists.clear()
                # MB is authoritative: hide iTunes/SoundCloud copies of the
                # title+type pairs it just added or refreshed.
                if mb_changed:
                    db.hide_lower_priority_title_duplicates(artist["id"], "musicbrainz")
            elif source == "itunes" and has_itunes:
                releases = await itunes.get_artist_releases(artist["itunes_artist_id"], artist_name=artist["name"])
                for rel in releases:
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
                        return_id=True,
                    )
                    if inserted:
                        new_titles.append(rel["title"])
                        if isinstance(inserted, int) and not isinstance(inserted, bool):
                            inserted_rows.append((inserted, rel["title"]))
                        itunes_changed = True
                # iTunes outranks SoundCloud: hide SC copies of these titles.
                if itunes_changed:
                    db.hide_lower_priority_title_duplicates(artist["id"], "itunes")
            elif source == "soundcloud" and has_sc:
                releases = await soundcloud.get_artist_releases(artist["soundcloud_permalink"])
                # Store every fetched track/playlist; dedup runs after the
                # import and hides lower-priority copies.
                for rel in releases:
                    inserted = db.add_release(
                        mbid=rel["mbid"],
                        artist_id=artist["id"],
                        title=rel["title"],
                        release_type=rel["type"],
                        release_date=rel.get("date", ""),
                        notified=0,
                        source="soundcloud",
                        soundcloud_track_id=rel.get("soundcloud_track_id", ""),
                        soundcloud_playlist_id=rel.get("soundcloud_playlist_id") or None,
                        artwork_url=rel.get("artwork_url", ""),
                        mb_url=rel.get("url", ""),
                        track_titles=rel.get("track_titles") or [],
                        return_id=True,
                    )
                    if inserted:
                        new_titles.append(rel["title"])
                        if isinstance(inserted, int) and not isinstance(inserted, bool):
                            inserted_rows.append((inserted, rel["title"]))

    if new_titles:
        # Everything fetched above is now registered. Run the shared dedup
        # pass so MB > iTunes > SoundCloud priority decides what stays visible.
        await _dedupe_releases()
        if inserted_rows:
            # Only report rows that are still visible. Duplicates registered
            # above and hidden by dedup must not look like new releases.
            visible_ids = {r["id"] for r in db.get_releases(artist_id=artist["id"])}
            visible_titles: list[str] = []
            seen_titles: set[str] = set()
            for row_id, title in inserted_rows:
                if row_id not in visible_ids or title in seen_titles:
                    continue
                seen_titles.add(title)
                visible_titles.append(title)
            new_titles = visible_titles
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

        # Final pass applies manual hide/unhide changes even when this scan
        # inserted no new rows.
        await _dedupe_releases()

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
    return await _dedupe_releases()


# --- Deduplication ---

# Bump when grouping/visibility rules change. The lifespan compares this to a
# meta row and runs one dedup pass on startup so existing libraries are
# repaired even when the user has not enabled startup_dedup.
DEDUP_POLICY_VERSION = 5


async def _dedupe_releases() -> dict:
    """Hide duplicate releases and restore ones that no longer are.

    Releases are grouped by (artist_id, normalized title, release-type
    family): Album and EP share a family because iTunes and SoundCloud often
    report EPs as albums, so an MB EP can replace their copy. Singles and
    Other stay distinct, so a single and an album that share a title never
    match. Same-source album/EP rows remain distinct; only a cross-source
    album-vs-EP pair counts as a duplicate. Within a group the
    highest-priority copy survives — MusicBrainz, then iTunes, then
    SoundCloud — and the rest are hidden. Hidden rows carry ``hidden_reason``
    so a later pass can tell dedup hides apart from manual ones, and rows
    this pass hid earlier but that no longer duplicate anything are restored.
    Titles that normalize to an empty string are ignored — they carry no
    information and must never be grouped.

    A second pass hides SoundCloud Singles that are really album/EP tracks.
    MusicBrainz tracklists are authoritative; other tracklists only match
    when the single landed with the album (within one day), so a genuine
    single released earlier stays visible.

    The whole pass runs in one ``db.batch()`` transaction: it cannot commit a
    scan's open batch and a failure leaves no partial dedup state.

    Returns {"hidden": newly hidden count, "restored": restored count,
    "total_hidden": all currently hidden releases}.
    """
    def _norm(title: str) -> str:
        return db.normalize_release_title(title)

    def _parse_full_date(value: str) -> date | None:
        """Parse a complete YYYY-MM-DD date; partial/unknown dates are None."""
        text = (value or "").strip()
        if len(text) != 10:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None

    hidden = 0
    restored = 0

    def _apply_title_groups(release_rows) -> None:
        """One title+type-family duplicate pass over a snapshot of rows."""
        nonlocal hidden, restored
        groups: dict[tuple[int, str, str], list[dict]] = {}
        for row in release_rows:
            norm = _norm(row["title"])
            if not norm:
                continue
            family = db.release_type_family(row["release_type"])
            key = (row["artist_id"], norm, family)
            groups.setdefault(key, []).append(dict(row))

        for group in groups.values():
            # Manual/legacy hides are user or unknown decisions, and tracklist
            # duplicates belong to the SoundCloud pass below.
            candidates = [
                r for r in group
                if r["is_visible"]
                or r["hidden_reason"] == db.HIDE_REASON_DEDUP_TITLE
            ]
            if not candidates:
                continue

            # Highest-priority source first. Within a tier prefer an
            # already-visible copy (so a same-source hidden duplicate is not
            # resurrected over the current winner), then the copy the user
            # explicitly restored, then insertion order.
            ordered = sorted(
                candidates,
                key=lambda r: (
                    db.source_priority(r["source"]),
                    0 if r["is_visible"] else 1,
                    0 if r["dedup_ignored"] else 1,
                    r["id"],
                ),
            )

            keepers_by_type: dict[str, dict] = {}
            keepers_by_family: dict[str, dict] = {}
            for row in ordered:
                rtype = db.normalize_release_type(row["release_type"])
                family = db.release_type_family(row["release_type"])

                # Exact-type copies always compete. Album vs EP only competes
                # across sources: iTunes/SoundCloud often report EPs as
                # albums, while MB can genuinely have both with one title.
                keeper = keepers_by_type.get(rtype)
                if keeper is None:
                    family_keeper = keepers_by_family.get(family)
                    if family_keeper is not None and db.are_duplicate_types(
                        row["release_type"], row["source"],
                        family_keeper["release_type"], family_keeper["source"],
                    ):
                        keeper = family_keeper

                # A copy the user explicitly restored stays visible even when
                # it duplicates a higher-priority copy.
                keep = keeper is None or (
                    row["is_visible"] and row["dedup_ignored"]
                )

                if not keep:
                    if row["is_visible"]:
                        db.hide_release_as_duplicate(
                            row["id"], db.HIDE_REASON_DEDUP_TITLE, keeper["id"]
                        )
                        row["is_visible"] = 0
                        hidden += 1
                    else:
                        # Already hidden as a duplicate; refresh the keeper.
                        db.hide_release_as_duplicate(
                            row["id"], db.HIDE_REASON_DEDUP_TITLE, keeper["id"]
                        )
                    continue

                if not row["is_visible"]:
                    # This pass hid it, but nothing duplicates it any more
                    # (rule removed, other copy deleted). Bring it back.
                    db.restore_dedup_release(row["id"])
                    row["is_visible"] = 1
                    row["hidden_reason"] = ""
                    restored += 1
                keepers_by_type.setdefault(rtype, row)
                keepers_by_family.setdefault(family, row)

    async with db.batch():
        conn = db.get_db()

        def _release_rows():
            return conn.execute(
                "SELECT id, artist_id, title, release_type, source, is_visible, "
                "hidden_reason, dedup_ignored "
                "FROM releases ORDER BY artist_id, first_seen_at, id"
            ).fetchall()

        _apply_title_groups(_release_rows())

        # Hide SoundCloud tracks that are really album/EP tracks. MusicBrainz
        # tracklists are authoritative and always win. Other sources only
        # count when the single landed with the album (within a day):
        # SoundCloud imports every upload as a Single, so a track uploaded on
        # release day must not look like a standalone single, while a single
        # released earlier stays visible.
        track_rows = conn.execute(
            "SELECT id, artist_id, source, release_date, track_titles "
            "FROM releases WHERE track_titles != '' "
            "AND lower(release_type) IN ('album', 'ep')"
        ).fetchall()
        album_tracks: dict[tuple[int, str], list[dict]] = {}
        for tr in track_rows:
            try:
                tracks = json.loads(tr["track_titles"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(tracks, list):
                continue
            candidate = {
                "id": tr["id"],
                "source": (tr["source"] or "").strip().casefold(),
                "date": _parse_full_date(tr["release_date"]),
            }
            for t in tracks:
                norm = _norm(t) if isinstance(t, str) else ""
                if norm:
                    album_tracks.setdefault(
                        (tr["artist_id"], norm), []
                    ).append(candidate)

        def _track_duplicate(row) -> dict | None:
            """Pick the album/EP row that makes this Single an album track."""
            candidates = album_tracks.get(
                (row["artist_id"], _norm(row["title"]))
            )
            if not candidates:
                return None
            # MB models genuine singles separately, so an MB album tracklist
            # is authoritative regardless of upload dates.
            for candidate in candidates:
                if candidate["source"] == "musicbrainz":
                    return candidate
            # Other sources have no separate single model. Only treat a track
            # as an album track when it landed with the album; earlier
            # uploads are standalone singles.
            single_date = _parse_full_date(row["release_date"])
            if single_date is None:
                return None
            best = None
            best_delta = None
            for candidate in candidates:
                album_date = candidate["date"]
                if album_date is None:
                    continue
                delta = abs((single_date - album_date).days)
                if delta <= 1 and (best_delta is None or delta < best_delta):
                    best = candidate
                    best_delta = delta
            return best

        # Re-evaluate rows this pass hid, so a tracklist that disappeared can
        # bring them back instead of leaving them hidden forever. Only
        # SoundCloud singles are eligible for track-list dedup: an album/EP
        # that happens to share a title with a track is a different release.
        restored_before_track = restored
        for row in conn.execute(
            "SELECT id, artist_id, title, release_type, release_date "
            "FROM releases WHERE hidden_reason = ?",
            (db.HIDE_REASON_DEDUP_TRACK,),
        ).fetchall():
            if db.normalize_release_type(row["release_type"]) != "single":
                db.restore_dedup_release(row["id"])
                restored += 1
                continue
            source = _track_duplicate(row)
            if source is None:
                db.restore_dedup_release(row["id"])
                restored += 1
            else:
                db.hide_release_as_duplicate(
                    row["id"], db.HIDE_REASON_DEDUP_TRACK, source["id"]
                )

        if album_tracks:
            sc_rows = conn.execute(
                "SELECT id, artist_id, title, release_date FROM releases "
                "WHERE source = 'soundcloud' AND lower(release_type) = 'single' "
                "AND is_visible = 1 AND dedup_ignored = 0"
            ).fetchall()
            for sr in sc_rows:
                source = _track_duplicate(sr)
                if source is not None:
                    db.hide_release_as_duplicate(
                        sr["id"], db.HIDE_REASON_DEDUP_TRACK, source["id"]
                    )
                    hidden += 1

        if restored > restored_before_track:
            # Restoring tracklist duplicates can expose same-title copies, so
            # close out with one more title pass. The end state must never
            # contain visible twins.
            _apply_title_groups(_release_rows())

        total_hidden = conn.execute(
            "SELECT COUNT(*) AS c FROM releases WHERE is_visible = 0"
        ).fetchone()["c"]

    return {"hidden": hidden, "restored": restored, "total_hidden": total_hidden}


async def _backfill_tracklists():
    """Fetch and store tracklists for MB album/EP releases that are missing them.

    Tracklists feed duplicate detection, so re-run dedup after filling rather
    than waiting for the next scan or startup pass.
    """
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
                tracks = await cache.cached(
                    f"tracks:{row['id']}",
                    cache.TTL_TRACKS,
                    lambda: musicbrainz.get_release_tracks(row["mbid"], row["artist_mbid"] or ""),
                )
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
        await _dedupe_releases()


async def _backfill_sc_tracklists():
    """Fetch and store tracklists for SoundCloud album/EP rows missing them.

    The playlist list response already carries tracks, so one fetch per
    artist covers all of that artist's albums. ID-only stubs are filled from
    the same response's full track list. Re-runs dedup afterwards because
    these tracklists are dedup input.
    """
    conn = db.get_db()
    rows = conn.execute(
        """SELECT r.id, r.soundcloud_playlist_id,
                  a.soundcloud_permalink
             FROM releases r
             JOIN artists a ON r.artist_id = a.id
            WHERE r.source = 'soundcloud'
              AND r.release_type IN ('Album', 'EP')
              AND (r.track_titles = '' OR r.track_titles IS NULL)
              AND r.soundcloud_playlist_id != ''
              AND a.soundcloud_permalink != ''"""
    ).fetchall()

    if not rows:
        return

    _add_log(
        "INFO",
        f"Backfilling tracklists for {len(rows)} SoundCloud release(s)...",
    )
    by_artist: dict[str, list] = {}
    for row in rows:
        by_artist.setdefault(row["soundcloud_permalink"], []).append(row)

    sem = asyncio.Semaphore(3)
    filled = 0

    async def _fill_artist(permalink: str, artist_rows) -> None:
        nonlocal filled
        async with sem:
            try:
                releases = await soundcloud.get_artist_releases(permalink)
            except Exception:
                return
            titles_by_playlist = {
                str(rel.get("soundcloud_playlist_id")): rel.get("track_titles") or []
                for rel in releases
                if rel.get("soundcloud_playlist_id")
            }
            for row in artist_rows:
                titles = titles_by_playlist.get(
                    str(row["soundcloud_playlist_id"])
                )
                if titles:
                    db.update_release_track_titles(row["id"], titles)
                    filled += 1

    for coro in asyncio.as_completed(
        [_fill_artist(p, rs) for p, rs in by_artist.items()]
    ):
        try:
            await coro
        except Exception:
            continue

    if filled:
        _add_log(
            "INFO",
            f"Backfilled tracklists for {filled} SoundCloud release(s)",
        )
        await _dedupe_releases()


async def _backfill_sc_urls():
    """Fetch canonical SoundCloud URLs for releases imported without one.

    Singles stored only their numeric track id (used as the uniqueness key),
    and older rows predate URL storage entirely, so their "View" link fell
    back to soundcloud.com/{artist}/{numeric_id}, which does not resolve.
    The v2 tracks/playlists endpoints return the real permalink_url, which
    also heals any row a scan's add_release backfill can't reach (tracks
    that have scrolled past the tracked-artist pagination window).
    """
    conn = db.get_db()
    rows = conn.execute(
        """SELECT id, soundcloud_track_id, soundcloud_playlist_id
             FROM releases
            WHERE source = 'soundcloud'
              AND (mb_url = '' OR mb_url IS NULL)"""
    ).fetchall()

    if not rows:
        return

    _add_log("INFO", f"Backfilling SoundCloud URLs for {len(rows)} release(s)...")
    sem = asyncio.Semaphore(4)
    filled = 0

    async def _fill_one(row) -> bool:
        async with sem:
            url = ""
            if row["soundcloud_playlist_id"]:
                url = await soundcloud.get_playlist_permalink_url(row["soundcloud_playlist_id"])
            elif row["soundcloud_track_id"]:
                url = await soundcloud.get_track_permalink_url(row["soundcloud_track_id"])
            if not url:
                return False
            db.update_release_mb_url(row["id"], url)
            return True

    for coro in asyncio.as_completed([_fill_one(r) for r in rows]):
        try:
            if await coro:
                filled += 1
        except Exception:
            continue
    if filled:
        _add_log("INFO", f"Backfilled URLs for {filled} SoundCloud release(s)")


# Bounded concurrency for tracklist fetches. Request starts stay spaced
# >=1s apart by the MusicBrainz pacer, so this only overlaps network
# latency — an artist with many new albums no longer pays ~2 paced
# requests per album strictly serially inside the scan loop.
_TRACKLIST_CONCURRENCY = 4


async def _fetch_tracklists_parallel(pending: list[tuple[int, str]], artist_mbid: str = ""):
    """Fetch tracklists for (release_id, release_mbid) pairs concurrently.

    Failures are swallowed per release, exactly like the previous inline
    await; a failed fetch is retried by the startup backfill on next boot.
    """
    if not pending:
        return
    sem = asyncio.Semaphore(_TRACKLIST_CONCURRENCY)

    async def _fill(release_id: int, release_mbid: str):
        async with sem:
            try:
                tracks = await cache.cached(
                    f"tracks:{release_id}",
                    cache.TTL_TRACKS,
                    lambda: musicbrainz.get_release_tracks(release_mbid, artist_mbid or ""),
                )
            except Exception:
                return
            if tracks:
                db.update_release_track_titles(release_id, [t["title"] for t in tracks])

    await asyncio.gather(*(_fill(rid, mbid) for rid, mbid in pending))


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
