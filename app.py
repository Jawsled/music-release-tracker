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
templates = Jinja2Templates(directory="templates")


# --- Request models ---

class ArtistSearchRequest(BaseModel):
    query: str

class ArtistAddRequest(BaseModel):
    mbid: str
    name: str
    disambiguation: str = ""


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
    results = await musicbrainz.search_artist(body.query)
    return results


@app.post("/api/artists")
async def add_artist(body: ArtistAddRequest):
    existing = db.get_artist_by_mbid(body.mbid)
    if existing:
        return {"status": "already_exists", "artist": existing}

    artist = db.add_artist(body.mbid, body.name, body.disambiguation)

        # Import all existing releases as already seen (notified=1)
    releases = await musicbrainz.get_artist_releases(body.mbid)
    count = 0
    for rel in releases:
        inserted = db.add_release(
            mbid=rel["mbid"],
            artist_id=artist["id"],
            title=rel["title"],
            release_type=rel["type"],
            release_date=rel["date"],
            notified=1,
            mb_url=rel.get("url", ""),
        )
        if inserted:
            count += 1

    return {"status": "added", "artist": artist, "releases_imported": count}


@app.delete("/api/artists/{artist_id}")
async def remove_artist(artist_id: int):
    db.remove_artist(artist_id)
    return {"status": "removed"}


@app.get("/api/artists/export")
async def export_artists():
    """Export tracked artists as a JSON file."""
    import json
    from fastapi.responses import Response

    artists = db.get_all_artists()
    payload = {
        "version": 1,
        "artists": [
            {"mbid": a["mbid"], "name": a["name"], "disambiguation": a["disambiguation"]}
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
    import json
    try:
        # Strip UTF-8 BOM if present (0xEF 0xBB 0xBF)
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
        if not mbid or not name:
            errors.append(f"Invalid entry: {entry}")
            continue
        existing = db.get_artist_by_mbid(mbid)
        if existing:
            skipped += 1
            continue
        try:
            db.add_artist(mbid, name, disambiguation)
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
    return db.get_releases(
        artist_id=artist_id,
        release_type=type,
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


@app.get("/api/releases/{release_mbid}/tracks")
async def get_release_tracks(release_mbid: str):
    """Fetch and return tracklist for a release group by MBID."""
    tracks = await musicbrainz.get_release_tracks(release_mbid)

    # Look up which artist this release belongs to, then find all single titles locally
    artist_id = db.get_artist_id_by_release_mbid(release_mbid)
    single_titles: set[str] = set()
    if artist_id is not None:
        raw_titles = db.get_artist_single_titles(artist_id)
        single_titles = {t.strip().lower() for t in raw_titles}

    # Annotate each track with has_single flag
    result_tracks = []
    for track in tracks:
        title_lower = track["title"].strip().lower()
        has_single = False
        if single_titles:
            # Exact match or fuzzy-ish: check if single title starts/ends with same words
            if title_lower in single_titles:
                has_single = True
            else:
                # Fuzzy: check if any single title is very close (e.g., ignores "the", "(edit)", etc.)
                for st in single_titles:
                    if _titles_match(title_lower, st):
                        has_single = True
                        break
        result_tracks.append({**track, "has_single": has_single})

    return {"mbid": release_mbid, "tracks": result_tracks}


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
async def check_releases(skip: int = Query(0)):
    async def event_stream():
        artists = db.get_all_artists()
        total = len(artists)

        _add_log("INFO", f"Starting scan for {total} artists (skip={skip})")

        if total == 0 or skip >= total:
            yield _sse({"type": "done", "message": "No artists to check.", "summary": [], "skip": skip, "total_checked": 0})
            return

        summary = []

        for i, artist in enumerate(artists[skip:], skip + 1):
            yield _sse({
                "type": "progress",
                "message": f"Checking artist {i} of {total}: {artist['name']}...",
                "current": i,
                "total": total,
            })

            try:
                releases = await musicbrainz.get_artist_releases(artist["mbid"])
                _add_log("INFO", f"Found {len(releases)} releases", artist=artist["name"])
            except Exception as e:
                error_msg = str(e) if str(e) else repr(e)
                # Extract useful info from httpx errors
                if hasattr(e, "response") and e.response is not None:
                    error_msg = f"HTTP {e.response.status_code}: {error_msg}"
                _add_log("ERROR", f"Failed to fetch releases: {error_msg}", artist=artist["name"])
                yield _sse({
                    "type": "error",
                    "message": f"Error checking {artist['name']}: {error_msg}",
                })
                continue

            new_count = 0
            new_titles = []
            for rel in releases:
                inserted = db.add_release(
                    mbid=rel["mbid"],
                    artist_id=artist["id"],
                    title=rel["title"],
                    release_type=rel["type"],
                    release_date=rel["date"],
                    notified=0,
                    mb_url=rel.get("url", ""),
                )
                if inserted:
                    new_count += 1
                    new_titles.append(f"{rel['title']} ({rel['type']})")

            if new_count > 0:
                summary.append({
                    "artist": artist["name"],
                    "new_releases": new_titles,
                })
                _add_log("INFO", f"Found {new_count} new release(s)", artist=artist["name"])

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
