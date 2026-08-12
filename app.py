from __future__ import annotations

import asyncio
import json
import logging
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import db
import musicbrainz
import itunes

HOST = "127.0.0.1"
PORT = 7070

# Setup application logging
logger = logging.getLogger("music-release-tracker")
logger.setLevel(logging.INFO)

# In-memory log storage (circular buffer)
_scan_logs: list[dict] = []
MAX_LOG_ENTRIES = 500


def _add_log(level: str, message: str, artist: str = "", detail: str = ""):
    """Add a log entry to the in-memory log store."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "level": level,
        "message": message,
        "artist": artist,
        "detail": detail,
    }
    _scan_logs.append(entry)
    # Keep only the last MAX_LOG_ENTRIES entries
    if len(_scan_logs) > MAX_LOG_ENTRIES:
        _scan_logs.pop(0)
    # Also write to console
    log_method = getattr(logger, level.lower(), logger.info)
    log_method(f"[{artist or 'SCAN'}] {message}" + (f" - {detail}" if detail else ""))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    webbrowser.open(f"http://{HOST}:{PORT}")
    yield


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

class ArtistAddRequest(BaseModel):
    source: str  # "musicbrainz" or "itunes"
    id: str  # mbid for musicbrainz, artistId for itunes
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

    if body.source == "itunes":
        return await _search_itunes()
    elif body.source == "musicbrainz":
        return await _search_mb()
    else:
        # Search both in parallel
        mb_results, it_results = await asyncio.gather(_search_mb(), _search_itunes())

        # Build lookup maps by normalized name
        mb_by_name: dict[str, dict] = {}
        for r in mb_results:
            key = _normalize_name(r["name"])
            mb_by_name[key] = r

        it_by_name: dict[str, dict] = {}
        for r in it_results:
            key = _normalize_name(r["name"])
            it_by_name[key] = r

        # Merge: combine matching names, keep all unique names
        merged: dict[str, dict] = {}
        for key, r in mb_by_name.items():
            merged[key] = {
                "name": r["name"],
                "mbid": r.get("mbid", ""),
                "itunes_artist_id": None,
                "disambiguation": r.get("disambiguation", ""),
                "type": r.get("type", ""),
                "country": r.get("country", ""),
                "score": r.get("score", 0),
                "source": "musicbrainz",
            }

        for key, r in it_by_name.items():
            if key in merged:
                # Name match: merge into existing entry
                merged[key]["itunes_artist_id"] = r.get("itunes_artist_id")
                merged[key]["source"] = "both"
                # Preserve iTunes country if not already set
                if not merged[key].get("country") and r.get("country"):
                    merged[key]["country"] = r["country"]
            else:
                merged[key] = {
                    "name": r["name"],
                    "mbid": "",
                    "itunes_artist_id": r.get("itunes_artist_id"),
                    "disambiguation": r.get("disambiguation", ""),
                    "type": "",
                    "country": r.get("country", ""),
                    "score": r.get("score", 0),
                    "source": "itunes",
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
    
    Strips iTunes classification suffixes like ' - Single' or ' - EP' and
    normalizes whitespace/punctuation for comparison.
    """
    import re
    # Strip iTunes classification suffixes
    title = re.sub(r'\s*-\s*(Single|EP)\s*$', '', title, flags=re.IGNORECASE)
    # Normalize: lowercase, strip punctuation, collapse whitespace
    title = title.lower().strip()
    title = re.sub(r'[^\w\s]', '', title)
    title = re.sub(r'\s+', ' ', title)
    return title


@app.post("/api/itunes/search")
async def search_itunes_artists(body: iTunesArtistSearchRequest):
    """Search for artists specifically on iTunes."""
    results = await itunes.search_artist(body.query, country=body.country)
    return results


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

        return {"status": "linked", "artist": artist, "releases_imported": count}

    # Create new artist entry
    artist = db.add_artist(
        mbid=body.id if body.source == "musicbrainz" else "",
        name=body.name,
        disambiguation=body.disambiguation,
        itunes_artist_id=int(body.id) if body.source == "itunes" else None
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
            )
            if inserted:
                count += 1
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


@app.get("/api/artists/export")
async def export_artists():
    """Export tracked artists as a JSON file."""
    import json
    from fastapi.responses import Response

    artists = db.get_all_artists()
    payload = {
        "version": 2,  # Bumped version due to itunes_artist_id inclusion
        "artists": [
            {
                "mbid": a["mbid"],
                "name": a["name"],
                "disambiguation": a["disambiguation"],
                "itunes_artist_id": a.get("itunes_artist_id"),
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
        if existing:
            skipped += 1
            continue
        try:
            db.add_artist(mbid or "", name, disambiguation, itunes_artist_id=itunes_artist_id)
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
):
    # Parse comma-separated type filter; empty means "all"
    type_list = [t.strip() for t in type.split(",")] if type else None
    return db.get_releases(
        artist_id=artist_id,
        release_type=type_list,
        unseen_only=unseen_only,
    )

@app.post("/api/releases/{release_id}/seen")
async def mark_seen(release_id: int):
    db.mark_release_seen(release_id)
    return {"status": "ok"}

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
    else:
        tracks = await musicbrainz.get_release_tracks(release_id)

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

        _add_log("INFO", f"Starting scan for {total} artist(s) (skip={skip})")

        if total == 0 or skip >= total:
            yield _sse({"type": "done", "message": "No artists to check.", "summary": [], "skip": skip, "total_checked": 0})
            return

        summary = []

        for i, artist in enumerate(artists[skip:], skip + 1):
            # Determine sources from whether mbid and/or itunes_artist_id is set
            has_mb = bool(artist.get("mbid"))
            has_itunes = bool(artist.get("itunes_artist_id"))
            sources = []
            if has_mb:
                sources.append("musicbrainz")
            if has_itunes:
                sources.append("itunes")

            # Default to musicbrainz if neither is set (legacy)
            if not sources:
                sources = ["musicbrainz"]

            yield _sse({
                "type": "progress",
                "message": f"Checking {', '.join(s.upper() for s in sources)} - artist {i} of {total}: {artist['name']}...",
                "current": i,
                "total": total,
            })

            try:
                new_titles = []
                for source in sources:
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
                            )
                            if inserted:
                                new_titles.append(rel["title"])
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

                total_sources = len(sources)
                _add_log("INFO", f"Found {len(new_titles)} new from {total_sources} source(s)", artist=artist["name"])

                if new_titles:
                    summary.append({
                        "artist": artist["name"],
                        "new_releases": new_titles,
                    })
                    _add_log("INFO", f"Found {len(new_titles)} new release(s)", artist=artist["name"])
            except Exception as e:
                error_msg = str(e) if str(e) else repr(e)
                # Extract useful info from httpx errors
                if hasattr(e, "response") and e.response is not None:
                    error_msg = f"HTTP {e.response.status_code}: {error_msg}"
                source_names = ', '.join(sources)
                _add_log("ERROR", f"Failed to fetch releases from {source_names}: {error_msg}", artist=artist["name"])
                yield _sse({
                    "type": "error",
                    "message": f"Error checking {artist['name']} ({source_names}): {error_msg}",
                })
                continue

        total_new = sum(len(s["new_releases"]) for s in summary)
        _add_log("INFO", f"Scan complete. Found {total_new} new release(s).")
        yield _sse({
            "type": "done",
            "message": f"Done! Found {total_new} new release(s).",
            "summary": summary,
        })

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/logs")
async def get_logs(limit: int = Query(100)):
    """Return recent scan log entries."""
    return _scan_logs[-limit:]


@app.post("/api/logs/clear")
async def clear_logs():
    """Clear the in-memory log store."""
    _scan_logs.clear()
    return {"status": "cleared"}


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# --- Entry point ---

if __name__ == "__main__":
    import uvicorn

    print(f"\n  Music Release Tracker")
    print(f"  Running at http://{HOST}:{PORT}\n")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
