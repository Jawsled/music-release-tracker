"""One-off verification for the tracklist superset fallback + dynamic TTL.

Run from the project root with the bundled venv python:
    & .venv-run\Scripts\python.exe verify_dedup_fallback.py

Uses a throwaway SQLite DB in ./.tmp-verify/ and stubs every upstream
fetcher, so the real database and network are never touched. Endpoint
coroutines are called directly (same thread as the SQLite connection).
"""
import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.getcwd())

# --- Isolate the DB BEFORE importing db/app consumers ---
_tmpdir = os.path.join(os.getcwd(), ".tmp-verify")
shutil.rmtree(_tmpdir, ignore_errors=True)
os.makedirs(_tmpdir, exist_ok=True)
import db  # noqa: E402

db.DB_PATH = os.path.join(_tmpdir, "verify.db")
db._conn = None
db.init_db()

import app  # noqa: E402
import cache  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ---------- 1. cache.tracks_ttl ----------
today = datetime.now(timezone.utc).date()
fresh, month = cache.TTL_TRACKS_FRESH, cache.TTL_TRACKS
check("ttl future date -> 1d", cache.tracks_ttl((today + timedelta(days=5)).isoformat()) == fresh)
check("ttl today -> 1d", cache.tracks_ttl(today.isoformat()) == fresh)
check("ttl 10d ago -> 1d", cache.tracks_ttl((today - timedelta(days=10)).isoformat()) == fresh)
check("ttl 45d ago -> 30d", cache.tracks_ttl((today - timedelta(days=45)).isoformat()) == month)
check("ttl empty -> 1d", cache.tracks_ttl("") == fresh)
check("ttl partial '2026-11' -> 1d", cache.tracks_ttl("2026-11") == fresh)
check("ttl garbage -> 1d", cache.tracks_ttl("not-a-date") == fresh)

# ---------- fixtures ----------
artist_id = db.add_artist(mbid="a-mbid", name="2hollis")["id"]


def mk_release(source, title, rtype, rdate, **kw):
    mbid = kw.pop("mbid", f"{source}-{title}")
    return db.add_release(
        mbid=mbid,
        artist_id=artist_id,
        title=title,
        release_type=rtype,
        release_date=rdate,
        notified=1,
        source=source,
        return_id=True,
        **kw,
    )


def rel_row(rid):
    return dict(db.get_db().execute("SELECT * FROM releases WHERE id = ?", (rid,)).fetchone())


def titles(n):
    base = ["Spring (Love Me)", "Skin", "The Lady In Red", "Pressure", "Human After All",
            "Sophie", "Sex", "Hurt", "Eyes Wide Closed", "Betrayal", "i2i", "Warhorse",
            "Today", "Heaven Speaker", "My One And Only", "Daisy"]
    return [{"number": str(i + 1), "title": t, "length": 0} for i, t in enumerate(base[:n])]


TRACKS_16 = titles(16)
TRACKS_3 = titles(3)

# Stub upstream fetchers (never touch the network)
MB_STUB = {}
ITUNES_STUB = {}


async def fake_mb_tracks(rg_id, main_mbid=""):
    return MB_STUB.get(rg_id, [])


async def fake_itunes_tracks(collection_id, country="us"):
    return ITUNES_STUB.get(str(collection_id), [])


async def fake_sc_tracks(track_id):
    return []


app.musicbrainz.get_release_tracks = fake_mb_tracks
app.itunes.get_release_tracks = fake_itunes_tracks
app.soundcloud.get_playlist_tracks = fake_sc_tracks
app.soundcloud.get_release_tracks = fake_sc_tracks


async def call_tracks(release_id):
    """Call the endpoint coroutine directly; return (status, payload_dict)."""
    resp = await app.get_release_tracks(release_id)
    if isinstance(resp, JSONResponse):
        return resp.status_code, json.loads(resp.body)
    return 200, resp


# ---------- 2. superset fallback, the user's exact scenario ----------
mb_id = mk_release("musicbrainz", "Pirouette", "Album", "2026-11-11", mbid="rg-pirouette")
it_id = mk_release("itunes", "Pirouette", "Album", "2026-11-11", mbid="1741919",
                   itunes_collection_id="1741919", artwork_url="x")
db.hide_release_as_duplicate(it_id, db.HIDE_REASON_DEDUP_TITLE, mb_id)

MB_STUB["rg-pirouette"] = TRACKS_3          # MB seeded with 3 pre-release tracks
ITUNES_STUB["1741919"] = TRACKS_16          # Apple Music already lists all 16

result = asyncio.run(app._duplicate_superset_tracks(rel_row(mb_id), TRACKS_3))
check("superset found for 3->16", result is not None)
if result:
    tracks, src = result
    check("superset serves 16 tracks", len(tracks) == 16, f"got {len(tracks)}")
    check("superset attributed to itunes", src == "itunes", f"got {src}")

# ---------- 3. endpoint integration ----------
status, data = asyncio.run(call_tracks("rg-pirouette"))
check("endpoint 200", status == 200, f"got {status}")
check("endpoint serves 16", len(data.get("tracks", [])) == 16, f"got {len(data.get('tracks', []))}")
check("endpoint names itunes", data.get("tracklist_source") == "itunes",
      f"got {data.get('tracklist_source')}")

# ---------- 4. self-revert once MB catches up ----------
MB_STUB["rg-pirouette"] = TRACKS_16
# Simulate the 1-day fresh-TTL expiry of the primary entry (in production the
# stub's real fetch would re-run then): drop the cached 3-track list.
db.get_db().execute("DELETE FROM api_cache WHERE key = ?", (f"tracks:{mb_id}",))
db.get_db().commit()
status, data = asyncio.run(call_tracks("rg-pirouette"))
check("revert: no strict superset once MB complete", data.get("tracklist_source") is None,
      f"got {data.get('tracklist_source')}")
check("revert: primary list served", len(data.get("tracks", [])) == 16,
      f"got {len(data.get('tracks', []))}")

# Fresh TTL re-reads MB within a day, so the stale 3-track window is bounded.
check("fresh release uses 1d TTL", cache.tracks_ttl("2026-11-11") == cache.TTL_TRACKS_FRESH)

# ---------- 5. edition missing a primary track is NOT hijacked ----------
mb2_id = mk_release("musicbrainz", "Deluxe Case", "Album", "2026-11-01", mbid="rg-deluxe")
it2_id = mk_release("itunes", "Deluxe Case", "Album", "2026-11-01", mbid="2000001",
                    itunes_collection_id="2000001")
db.hide_release_as_duplicate(it2_id, db.HIDE_REASON_DEDUP_TITLE, mb2_id)
base3 = titles(3)
deluxe_missing = [t for t in titles(16) if t["title"] != base3[2]["title"]]
MB_STUB["rg-deluxe"] = base3
ITUNES_STUB["2000001"] = deluxe_missing  # 15 tracks but missing one the MB list has
res2 = asyncio.run(app._duplicate_superset_tracks(rel_row(mb2_id), base3))
check("edition missing a base track -> no fallback", res2 is None)

# ---------- 6. equal list -> no fallback ----------
ITUNES_STUB["2000001"] = base3
res3 = asyncio.run(app._duplicate_superset_tracks(rel_row(mb2_id), base3))
check("equal list -> no fallback", res3 is None)

# ---------- 7. manual hides are never consulted ----------
mb4_id = mk_release("musicbrainz", "Manual Hide", "Album", "2026-10-01", mbid="rg-manual")
it4_id = mk_release("itunes", "Manual Hide", "Album", "2026-10-01", mbid="2000002",
                    itunes_collection_id="2000002")
db.set_release_visible(it4_id, False)  # manual hide
MB_STUB["rg-manual"] = titles(2)
ITUNES_STUB["2000002"] = TRACKS_16
res4 = asyncio.run(app._duplicate_superset_tracks(rel_row(mb4_id), titles(2)))
check("manual hide excluded", res4 is None)

# ---------- 8. empty primary list accepts fuller duplicate ----------
mb5_id = mk_release("musicbrainz", "Empty Primary", "Album", "2026-11-10", mbid="rg-empty")
it5_id = mk_release("itunes", "Empty Primary", "Album", "2026-11-10", mbid="2000003",
                    itunes_collection_id="2000003")
db.hide_release_as_duplicate(it5_id, db.HIDE_REASON_DEDUP_TITLE, mb5_id)
MB_STUB["rg-empty"] = []  # MB has no tracks yet
ITUNES_STUB["2000003"] = titles(5)
res5 = asyncio.run(app._duplicate_superset_tracks(rel_row(mb5_id), []))
check("empty primary accepts fuller duplicate",
      res5 is not None and len(res5[0]) == 5,
      f"got {res5 if res5 is None else len(res5[0])}")

# ---------- 9. different family (single vs album) never matches ----------
mb6_id = mk_release("musicbrainz", "Family Guard", "Album", "2026-11-09", mbid="rg-family")
it6_id = mk_release("itunes", "Family Guard", "Single", "2026-11-09", mbid="2000004",
                    itunes_collection_id="2000004")
db.hide_release_as_duplicate(it6_id, db.HIDE_REASON_DEDUP_TITLE, mb6_id)
ITUNES_STUB["2000004"] = titles(9)
res6 = asyncio.run(app._duplicate_superset_tracks(rel_row(mb6_id), titles(3)))
check("single vs album family -> no fallback", res6 is None)

# ---------- 10. different title never matches ----------
it7_id = mk_release("itunes", "Pirouette Deluxe", "Album", "2026-11-11", mbid="2000005",
                    itunes_collection_id="2000005")
db.hide_release_as_duplicate(it7_id, db.HIDE_REASON_DEDUP_TITLE, mb_id)
ITUNES_STUB["2000005"] = TRACKS_16
res7 = asyncio.run(app._duplicate_superset_tracks(rel_row(mb_id), TRACKS_3))
check("different-title hidden copy ignored (falls back to Pirouette itunes match)",
      res7 is not None and len(res7[0]) == 16 and res7[1] == "itunes")

# ---------- 11. normal MB-only album unaffected ----------
mb7_id = mk_release("musicbrainz", "Plain Album", "Album", "2025-01-15", mbid="rg-plain")
MB_STUB["rg-plain"] = titles(8)
status, data = asyncio.run(call_tracks("rg-plain"))
check("plain album: own list, no attribution",
      len(data["tracks"]) == 8 and data.get("tracklist_source") is None,
      f"got {len(data['tracks'])} / {data.get('tracklist_source')}")

# ---------- 12. upstream failure still errors (no silent fallback masking it) ----------
async def boom(rg_id, main_mbid=""):
    raise RuntimeError("MB 503")


mb8_id = mk_release("musicbrainz", "Error Case", "Album", "2025-02-02", mbid="rg-err")
app.musicbrainz.get_release_tracks = boom
status, data = asyncio.run(call_tracks("rg-err"))
check("upstream failure -> error payload",
      status >= 500 and "error" in data, f"got {status} / {data.keys()}")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("All checks passed.")
