from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent / "music-release-tracker.db")

# Bump when the artist export gains a field so importers can adapt.
ARTISTS_EXPORT_VERSION = 3
# Number of automatic artist JSON snapshots kept in BACKUP/.
ARTIST_BACKUP_KEEP = 30

_conn: sqlite3.Connection | None = None

# While > 0, write helpers defer commits so a whole unit of work (one
# artist's scan/import) lands in a single transaction. See batch().
_batch_depth = 0


class _TaskReentrantLock:
    """Async lock a task may re-acquire (batch() may nest in one call path).

    Different tasks still serialize — two concurrent scans never interleave
    their uncommitted rows into one transaction — while a task that opens
    a second batch() inside an existing one simply joins it.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    async def __aenter__(self):
        task = asyncio.current_task()
        if self._owner is not None and self._owner is task:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner = task
        self._depth = 1
        return self

    async def __aexit__(self, *exc):
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()
        return False


_batch_lock = _TaskReentrantLock()


@asynccontextmanager
async def batch():
    """Group writes into one commit (typically per artist / per import).

    Nests safely: only the outermost exit commits, and any exception rolls
    the whole group back. Reads on the same connection still see the
    group's uncommitted rows, so in-batch dedup checks stay correct.
    """
    global _batch_depth
    conn = get_db()
    async with _batch_lock:
        _batch_depth += 1
        try:
            yield
        except BaseException:
            _batch_depth -= 1
            if _batch_depth == 0:
                conn.rollback()
            raise
        else:
            _batch_depth -= 1
            if _batch_depth == 0:
                conn.commit()


def _maybe_commit():
    """Commit unless a batch() group is open (it commits on exit)."""
    if _batch_depth == 0:
        get_db().commit()


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.execute("PRAGMA journal_mode = WAL")
        # Local single-user app: NORMAL is durability-safe under WAL (a power
        # cut can lose the last transaction, never corrupt the database) and
        # drops the fsync-per-commit cost. The rest keep hot reads in memory.
        _conn.execute("PRAGMA synchronous = NORMAL")
        _conn.execute("PRAGMA temp_store = MEMORY")
        _conn.execute("PRAGMA cache_size = -20000")  # ~20 MB page cache
        _conn.execute("PRAGMA mmap_size = 268435456")  # 256 MB
    return _conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS artists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mbid TEXT,
            name TEXT NOT NULL,
            disambiguation TEXT DEFAULT '',
            itunes_artist_id INTEGER,
            added_at TEXT NOT NULL
        );

                CREATE TABLE IF NOT EXISTS releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mbid TEXT NOT NULL,
            artist_id INTEGER NOT NULL,
            source TEXT DEFAULT 'musicbrainz',
            title TEXT NOT NULL,
            release_type TEXT NOT NULL,
            release_date TEXT DEFAULT '',
            first_seen_at TEXT NOT NULL,
            notified INTEGER DEFAULT 0,
            release_day_notified INTEGER DEFAULT 0,
            mb_url TEXT DEFAULT '',
            itunes_collection_id TEXT,
            artwork_url TEXT DEFAULT '',
            credits TEXT DEFAULT '',
            FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS api_cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );
    """)

    migrations = [
        "ALTER TABLE releases ADD COLUMN source TEXT DEFAULT 'musicbrainz'",
        "ALTER TABLE releases ADD COLUMN itunes_collection_id TEXT",
        "ALTER TABLE artists ADD COLUMN itunes_artist_id INTEGER",
        "ALTER TABLE releases ADD COLUMN credits TEXT DEFAULT ''",
        "ALTER TABLE artists ADD COLUMN soundcloud_permalink TEXT",
        "ALTER TABLE releases ADD COLUMN soundcloud_track_id TEXT",
        "ALTER TABLE releases ADD COLUMN soundcloud_playlist_id TEXT",
        "ALTER TABLE releases ADD COLUMN track_titles TEXT DEFAULT ''",
        "ALTER TABLE releases ADD COLUMN is_visible INTEGER DEFAULT 1",
        "ALTER TABLE releases ADD COLUMN hidden_reason TEXT DEFAULT ''",
        "ALTER TABLE releases ADD COLUMN duplicate_of INTEGER",
        "ALTER TABLE releases ADD COLUMN dedup_ignored INTEGER DEFAULT 0",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Migration: add release_day_notified column for existing databases
    try:
        conn.execute("ALTER TABLE releases ADD COLUMN release_day_notified INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

        # Migration: add mb_url column for existing databases
    try:
        conn.execute("ALTER TABLE releases ADD COLUMN mb_url TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Migration: add artwork_url column for existing databases
    try:
        conn.execute("ALTER TABLE releases ADD COLUMN artwork_url TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    conn.commit()

    # Indexes for the hot lookup paths (scan dedup, unseen queries, feed
    # ordering). IF NOT EXISTS keeps this a no-op on databases that already
    # carry them, while fresh installs get them from the start.
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_releases_artist ON releases(artist_id)",
        "CREATE INDEX IF NOT EXISTS idx_releases_artist_mbid ON releases(artist_id, mbid)",
        "CREATE INDEX IF NOT EXISTS idx_releases_artist_itunes ON releases(artist_id, itunes_collection_id)",
        "CREATE INDEX IF NOT EXISTS idx_releases_artist_sc_track ON releases(artist_id, soundcloud_track_id)",
        "CREATE INDEX IF NOT EXISTS idx_releases_artist_sc_playlist ON releases(artist_id, soundcloud_playlist_id)",
        "CREATE INDEX IF NOT EXISTS idx_releases_unseen ON releases(notified, is_visible)",
        "CREATE INDEX IF NOT EXISTS idx_releases_visible_date ON releases(is_visible, release_date, first_seen_at)",
        "CREATE INDEX IF NOT EXISTS idx_releases_hidden_reason ON releases(is_visible, hidden_reason)",
        "CREATE INDEX IF NOT EXISTS idx_artists_mbid ON artists(mbid)",
        "CREATE INDEX IF NOT EXISTS idx_artists_itunes ON artists(itunes_artist_id)",
        "CREATE INDEX IF NOT EXISTS idx_artists_sc ON artists(soundcloud_permalink)",
    ]
    for sql in indexes:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # index exists under a different definition
    conn.commit()

    # Fold any WAL content back into the main .db file so that copying just
    # music-release-tracker.db (without its -wal/-shm sidecars) always
    # carries the full data. Cheap and safe to run on every startup.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass

    # Drop cache entries nothing has touched in months (deleted releases leave
    # orphans behind) so the cache can't grow without bound.
    try:
        cache_prune()
    except sqlite3.OperationalError:
        pass


def get_counts() -> dict[str, int]:
    """Return artist/release totals (for startup diagnostics)."""
    conn = get_db()
    artists = conn.execute("SELECT COUNT(*) c FROM artists").fetchone()["c"]
    releases = conn.execute("SELECT COUNT(*) c FROM releases").fetchone()["c"]
    return {"artists": artists, "releases": releases}


def backup_database(keep: int = 5) -> str | None:
    """Save a timestamped, consistent snapshot of the database into BACKUP/.

    Uses SQLite's online backup API instead of copying the file. In WAL mode a
    plain copy can catch a half-checkpointed main file, or miss the -wal
    sidecar entirely, producing a corrupt or empty backup. The copy is written
    to a temporary file, integrity-checked, then renamed into place so a failed
    backup never leaves a broken .db behind.

    Never raises: returns the backup path, or None when anything goes wrong.
    """
    from datetime import datetime
    try:
        backup_dir = Path(DB_PATH).parent / "BACKUP"
        backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = backup_dir / f"music-release-tracker-{stamp}.db"
        tmp = backup_dir / f"{dest.name}.tmp"
        try:
            tmp.unlink()
        except OSError:
            pass

        try:
            dest_conn = sqlite3.connect(tmp)
            try:
                get_db().backup(dest_conn)
                status = dest_conn.execute("PRAGMA integrity_check").fetchone()[0]
                if status != "ok":
                    raise sqlite3.DatabaseError(f"backup integrity check failed: {status}")
            finally:
                dest_conn.close()
            tmp.replace(dest)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

        existing = sorted(backup_dir.glob("music-release-tracker-*.db"))
        for old in existing[:-keep] if len(existing) > keep else []:
            try:
                old.unlink()
            except OSError:
                pass
        return str(dest)
    except Exception:
        return None


def _artists_export_payload(artists: list[dict]) -> dict:
    """Build the importable artist export payload."""
    return {
        "version": ARTISTS_EXPORT_VERSION,
        "artists": [
            {
                "mbid": a.get("mbid", "") or "",
                "name": a.get("name", ""),
                "disambiguation": a.get("disambiguation", "") or "",
                "itunes_artist_id": a.get("itunes_artist_id"),
                "soundcloud_permalink": a.get("soundcloud_permalink"),
            }
            for a in artists
        ],
    }


def artists_export_payload() -> dict:
    """Artist export payload, shared by the API endpoint and backups."""
    return _artists_export_payload(get_all_artists())


def _backup_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def backup_artists_json(keep: int = ARTIST_BACKUP_KEEP) -> str | None:
    """Save a timestamped JSON snapshot of tracked artists into BACKUP/.

    Called before init_db() runs migrations at startup, so a future schema
    change can never strand the artist list, and refreshed once a day while
    the app stays open. The payload matches /api/artists/export, so a
    snapshot can be re-imported from the Artists page.

    Never raises: returns the path, or None when there is nothing to save or
    anything goes wrong.
    """
    from datetime import datetime
    db_file = Path(DB_PATH)
    try:
        # A fresh install has no database yet: don't create one just to find
        # an empty artist list.
        if not db_file.exists() or db_file.stat().st_size == 0:
            return None
        artists = get_all_artists()
        if not artists:
            return None
        payload = _artists_export_payload(artists)

        backup_dir = db_file.parent / "BACKUP"
        backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = backup_dir / f"music-release-tracker-artists-{stamp}.json"
        if dest.exists():
            # Two snapshots in the same second (startup + immediate refresh).
            dest = backup_dir / (
                f"music-release-tracker-artists-{stamp}-"
                f"{datetime.now().strftime('%f')}.json"
            )
        tmp = backup_dir / f"{dest.name}.tmp"
        try:
            tmp.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            tmp.replace(dest)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

        existing = sorted(
            backup_dir.glob("music-release-tracker-artists-*.json"),
            key=_backup_mtime,
        )
        for old in existing[:-keep] if len(existing) > keep else []:
            try:
                old.unlink()
            except OSError:
                pass
        return str(dest)
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Artists ---

def add_artist(
    mbid: str,
    name: str,
    disambiguation: str = "",
    itunes_artist_id: int | None = None,
    soundcloud_permalink: str | None = None,
) -> dict:
    conn = get_db()
    conn.execute(
        "INSERT INTO artists (mbid, name, disambiguation, itunes_artist_id, soundcloud_permalink, added_at) VALUES (?, ?, ?, ?, ?, ?)",
        (mbid, name, disambiguation, itunes_artist_id, soundcloud_permalink, _now_iso()),
    )
    _maybe_commit()
    if soundcloud_permalink:
        row = conn.execute("SELECT * FROM artists WHERE soundcloud_permalink = ?", (soundcloud_permalink,)).fetchone()
    elif itunes_artist_id:
        row = conn.execute("SELECT * FROM artists WHERE itunes_artist_id = ?", (itunes_artist_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM artists WHERE mbid = ?", (mbid,)).fetchone()
    return dict(row)


def remove_artist(artist_id: int):
    conn = get_db()
    conn.execute("DELETE FROM artists WHERE id = ?", (artist_id,))
    _maybe_commit()


def get_all_artists() -> list[dict]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM artists ORDER BY name").fetchall()
    artists = [dict(r) for r in rows]
    # SQLite's BINARY collation sorts case-sensitively (all-caps before all
    # lowercase) and scatters accented names by codepoint — sort naturally.
    artists.sort(key=lambda a: _artist_sort_key(a.get("name", "")))
    return artists


# Manual transliterations for letters with no NFKD decomposition.
_NON_DECOMPOSABLE = {
    "ø": "o",
    "ł": "l",
    "æ": "ae",
    "œ": "oe",
    "đ": "d",
    "ð": "d",
    "þ": "th",
    "ı": "i",
    "ŋ": "n",
}


def _artist_sort_key(name: str) -> str:
    """Natural sort key: case-insensitive, diacritics stripped.

    'aszewo' sorts with the A's, 'XYLØ' with the X's, 'hanna ögonsten'
    with the H's — instead of SQLite byte order (Zedd before aszewo).
    """
    import unicodedata
    folded = (name or "").casefold()
    decomposed = unicodedata.normalize("NFKD", folded)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(_NON_DECOMPOSABLE.get(c, c) for c in stripped)


def get_artist_by_id(artist_id: int) -> dict | None:
    """Look up an artist by internal row ID (explicit link target)."""
    conn = get_db()
    row = conn.execute("SELECT * FROM artists WHERE id = ?", (artist_id,)).fetchone()
    return dict(row) if row else None


def get_artist_by_mbid(mbid: str) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM artists WHERE mbid = ?", (mbid,)).fetchone()
    return dict(row) if row else None


def get_artist_by_itunes_id(itunes_artist_id: int) -> dict | None:
    """Look up an artist by their iTunes artist ID."""
    conn = get_db()
    row = conn.execute("SELECT * FROM artists WHERE itunes_artist_id = ?", (itunes_artist_id,)).fetchone()
    return dict(row) if row else None


def get_artist_by_soundcloud_permalink(permalink: str) -> dict | None:
    """Look up an artist by their SoundCloud permalink."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM artists WHERE soundcloud_permalink = ?", (permalink,)
    ).fetchone()
    return dict(row) if row else None


def link_artist(
    artist_id: int,
    mbid: str,
    itunes_artist_id: int | None,
    soundcloud_permalink: str | None = None,
) -> dict:
    """Link a MusicBrainz, iTunes, or SoundCloud ID to an existing artist entry."""
    conn = get_db()
    if mbid:
        conn.execute(
            "UPDATE artists SET mbid = ? WHERE id = ?",
            (mbid, artist_id),
        )
    if itunes_artist_id:
        conn.execute(
            "UPDATE artists SET itunes_artist_id = ? WHERE id = ?",
            (itunes_artist_id, artist_id),
        )
    if soundcloud_permalink:
        conn.execute(
            "UPDATE artists SET soundcloud_permalink = ? WHERE id = ?",
            (soundcloud_permalink, artist_id),
        )
    _maybe_commit()
    row = conn.execute("SELECT * FROM artists WHERE id = ?", (artist_id,)).fetchone()
    return dict(row)


def unlink_artist_itunes(artist_id: int) -> dict:
    """Remove iTunes ID from an artist entry and delete associated iTunes releases."""
    conn = get_db()
    # Delete all iTunes releases for this artist
    conn.execute(
        "DELETE FROM releases WHERE artist_id = ? AND source = 'itunes'",
        (artist_id,),
    )
    # Clear the iTunes ID
    conn.execute(
        "UPDATE artists SET itunes_artist_id = NULL WHERE id = ?",
        (artist_id,),
    )
    _maybe_commit()
    row = conn.execute("SELECT * FROM artists WHERE id = ?", (artist_id,)).fetchone()
    return dict(row)


def unlink_artist_mb(artist_id: int) -> dict:
    """Remove MusicBrainz ID from an artist entry and delete associated MB releases."""
    conn = get_db()
    # Delete all MusicBrainz releases for this artist
    conn.execute(
        "DELETE FROM releases WHERE artist_id = ? AND source = 'musicbrainz'",
        (artist_id,),
    )
    # Clear the MB ID
    conn.execute(
        "UPDATE artists SET mbid = NULL WHERE id = ?",
        (artist_id,),
    )
    _maybe_commit()
    row = conn.execute("SELECT * FROM artists WHERE id = ?", (artist_id,)).fetchone()
    return dict(row)


def unlink_artist_soundcloud(artist_id: int) -> dict:
    """Remove SoundCloud permalink from an artist entry and delete associated SC releases."""
    conn = get_db()
    conn.execute(
        "DELETE FROM releases WHERE artist_id = ? AND source = 'soundcloud'",
        (artist_id,),
    )
    conn.execute(
        "UPDATE artists SET soundcloud_permalink = NULL WHERE id = ?",
        (artist_id,),
    )
    _maybe_commit()
    row = conn.execute("SELECT * FROM artists WHERE id = ?", (artist_id,)).fetchone()
    return dict(row)


def update_artist_disambiguation(artist_id: int, disambiguation: str) -> dict | None:
    """Set a tracked artist's disambiguation note (free text, may be empty)."""
    conn = get_db()
    conn.execute(
        "UPDATE artists SET disambiguation = ? WHERE id = ?",
        ((disambiguation or "").strip()[:200], artist_id),
    )
    _maybe_commit()
    row = conn.execute("SELECT * FROM artists WHERE id = ?", (artist_id,)).fetchone()
    return dict(row) if row else None


# --- Releases ---

def _track_titles_json(track_titles: list[str] | None) -> str:
    """JSON-encode a tracklist, dropping blanks; "" when there is none."""
    titles = [str(t).strip() for t in (track_titles or []) if str(t).strip()]
    return json.dumps(titles) if titles else ""


def add_release(
    mbid: str,
    artist_id: int,
    title: str,
    release_type: str,
    release_date: str,
    notified: int = 0,
    mb_url: str = "",
    source: str = "musicbrainz",
    itunes_collection_id: str | None = None,
    artwork_url: str = "",
    credits: str = "",
    soundcloud_track_id: str | None = None,
    soundcloud_playlist_id: str | None = None,
    return_id: bool = False,
    track_titles: list[str] | None = None,
) -> bool | int:
    """Insert a release, or update release_date if it already exists.
    
    Returns True if inserted or updated, False if unchanged. With
    ``return_id=True``, returns the row id for inserts/updates instead, so the
    caller can check whether dedup left that exact row visible.
    Updates ensure bootleg/unofficial dates get replaced with official dates
    on the next sync.

    credits: JSON string of credited artists (from MusicBrainz artist-credit).
    Existing rows are silently backfilled when credit data arrives or changes —
    this does NOT count as a new release. Missing/changed mb_url is backfilled
    the same way, so rows imported before a source stored URLs still get a
    working link on the next sync.

    track_titles: album/EP tracklist used by duplicate detection. Backfilled
    silently when supplied; an empty fetch never clears an existing list.
    """
    conn = get_db()
    # For non-MB releases, use their own ID as uniqueness key
    if source == "itunes":
        unique_key = itunes_collection_id or mbid
    elif source == "soundcloud":
        unique_key = soundcloud_playlist_id or soundcloud_track_id or mbid
    else:
        unique_key = mbid

    # Build the uniqueness query based on available identifiers
    if soundcloud_playlist_id:
        existing = conn.execute(
            "SELECT id, release_date, credits, mb_url, track_titles FROM releases WHERE artist_id = ? AND soundcloud_playlist_id = ?",
            (artist_id, soundcloud_playlist_id),
        ).fetchone()
    elif soundcloud_track_id:
        existing = conn.execute(
            "SELECT id, release_date, credits, mb_url, track_titles FROM releases WHERE artist_id = ? AND soundcloud_track_id = ?",
            (artist_id, soundcloud_track_id),
        ).fetchone()
    elif itunes_collection_id:
        existing = conn.execute(
            "SELECT id, release_date, credits, mb_url, track_titles FROM releases WHERE artist_id = ? AND (mbid = ? OR itunes_collection_id = ?)",
            (artist_id, unique_key, unique_key),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT id, release_date, credits, mb_url, track_titles FROM releases WHERE artist_id = ? AND mbid = ?",
            (artist_id, unique_key),
        ).fetchone()

    if existing:
        changed = False
        # Update release_date if it differs (e.g., bootleg date -> official date)
        if existing["release_date"] != release_date:
            conn.execute(
                "UPDATE releases SET release_date = ? WHERE id = ?",
                (release_date, existing["id"]),
            )
            changed = True
        # Silently backfill credits when they arrive/changed (not counted as new)
        if credits and existing["credits"] != credits:
            conn.execute(
                "UPDATE releases SET credits = ? WHERE id = ?",
                (credits, existing["id"]),
            )
        # Backfill/refresh mb_url when missing or changed (not counted as new).
        # Rows imported before source URLs were stored (e.g. SoundCloud tracks)
        # get their real permalink URL on the next sync.
        if mb_url and existing["mb_url"] != mb_url:
            conn.execute(
                "UPDATE releases SET mb_url = ? WHERE id = ?",
                (mb_url, existing["id"]),
            )
        # Store/refresh the tracklist when the source supplied one. This is
        # a silent backfill like credits, not a new-release change.
        track_titles_json = _track_titles_json(track_titles)
        if track_titles_json and existing["track_titles"] != track_titles_json:
            conn.execute(
                "UPDATE releases SET track_titles = ? WHERE id = ?",
                (track_titles_json, existing["id"]),
            )
        _maybe_commit()
        if return_id and changed:
            return existing["id"]
        return changed  # True only for inserts / date updates

    cursor = conn.execute(
        """INSERT INTO releases
           (mbid, artist_id, source, title, release_type, release_date, first_seen_at, notified, mb_url, itunes_collection_id, artwork_url, credits, soundcloud_track_id, soundcloud_playlist_id, track_titles)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (unique_key, artist_id, source, title, release_type, release_date, _now_iso(), notified, mb_url, itunes_collection_id, artwork_url, credits, soundcloud_track_id, soundcloud_playlist_id, _track_titles_json(track_titles)),
    )
    _maybe_commit()
    return cursor.lastrowid if return_id else True


def _release_filters(
    artist_id: int | None = None,
    release_type: list[str] | None = None,
    unseen_only: bool = False,
    include_hidden: bool = False,
    hidden_only: bool = False,
) -> tuple[str, list]:
    """Build the shared WHERE clause + params for release queries.

    Used by both get_releases() (page fetch) and count_releases() so the feed
    total and its rows can never disagree about which releases match.
    """
    where: list[str] = []
    params: list = []

    if hidden_only:
        where.append("r.is_visible = 0")
    elif not include_hidden:
        where.append("r.is_visible = 1")
    if artist_id is not None:
        where.append("r.artist_id = ?")
        params.append(artist_id)
    if release_type:
        placeholders = ", ".join("?" for _ in release_type)
        where.append(f"r.release_type IN ({placeholders})")
        params.extend(release_type)
    if unseen_only:
        where.append("r.notified = 0")

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return clause, params


# Feed ordering. id is appended as a tiebreaker so the ordering is total,
# which is what makes the cursor below unambiguous when two releases share a
# date and a first_seen_at.
_RELEASE_ORDER = " ORDER BY r.release_date DESC, r.first_seen_at DESC, r.id DESC"


def get_releases(
    artist_id: int | None = None,
    release_type: list[str] | None = None,
    unseen_only: bool = False,
    include_hidden: bool = False,
    hidden_only: bool = False,
    limit: int | None = None,
    cursor: tuple[str, str, int] | None = None,
) -> list[dict]:
    """Fetch releases, newest first.

    ``limit``/``cursor`` drive feed pagination. ``cursor`` is the
    (release_date, first_seen_at, id) of the last row already shown; only rows
    strictly older than it are returned. A cursor (rather than OFFSET) means
    hiding/marking a visible card can't make the next page skip a release.
    Without ``limit`` every matching row is returned (internal callers).
    """
    conn = get_db()
    clause, params = _release_filters(
        artist_id=artist_id,
        release_type=release_type,
        unseen_only=unseen_only,
        include_hidden=include_hidden,
        hidden_only=hidden_only,
    )

    query = (
        "SELECT r.*, a.name as artist_name, a.mbid as artist_mbid, "
        "a.soundcloud_permalink as artist_soundcloud_permalink, "
        "d.title as duplicate_title, "
        "d.release_type as duplicate_release_type, "
        "d.release_date as duplicate_release_date, "
        "d.source as duplicate_source, "
        "d.is_visible as duplicate_is_visible "
        "FROM releases r JOIN artists a ON r.artist_id = a.id "
        "LEFT JOIN releases d ON d.id = r.duplicate_of" + clause
    )

    if cursor is not None:
        c_date, c_time, c_id = cursor
        query += (
            " AND (r.release_date < ?"
            " OR (r.release_date = ? AND r.first_seen_at < ?)"
            " OR (r.release_date = ? AND r.first_seen_at = ? AND r.id < ?))"
        )
        params.extend([c_date, c_date, c_time, c_date, c_time, c_id])

    query += _RELEASE_ORDER

    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))

    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def count_releases(
    artist_id: int | None = None,
    release_type: list[str] | None = None,
    unseen_only: bool = False,
    include_hidden: bool = False,
    hidden_only: bool = False,
) -> int:
    """Total releases matching the same filters get_releases() uses."""
    conn = get_db()
    clause, params = _release_filters(
        artist_id=artist_id,
        release_type=release_type,
        unseen_only=unseen_only,
        include_hidden=include_hidden,
        hidden_only=hidden_only,
    )
    row = conn.execute("SELECT COUNT(*) AS c FROM releases r" + clause, params).fetchone()
    return row["c"] if row else 0


def mark_release_seen(release_id: int):
    """Mark a single release as seen (notified)."""
    conn = get_db()
    conn.execute("UPDATE releases SET notified = 1 WHERE id = ?", (release_id,))
    _maybe_commit()


# Why a release is hidden. '' on legacy hidden rows (before provenance existed)
# and on visible rows; treated as "unknown" so dedup never touches it.
HIDE_REASON_MANUAL = "manual"
HIDE_REASON_DEDUP_TITLE = "dedup_title"
HIDE_REASON_DEDUP_TRACK = "dedup_track"


def set_release_visible(release_id: int, visible: bool):
    """Show or hide a release in the feed.

    Showing marks the row as user-preferred (`dedup_ignored`) and clears any
    dedup provenance, so a later dedup pass keeps it visible and removes other
    copies instead. Hiding here is always a manual hide, never a dedup one.
    """
    conn = get_db()
    if visible:
        conn.execute(
            "UPDATE releases SET is_visible = 1, hidden_reason = '', "
            "duplicate_of = NULL, dedup_ignored = 1 WHERE id = ?",
            (release_id,),
        )
    else:
        conn.execute(
            "UPDATE releases SET is_visible = 0, hidden_reason = ?, "
            "duplicate_of = NULL, dedup_ignored = 0 WHERE id = ?",
            (HIDE_REASON_MANUAL, release_id),
        )
    _maybe_commit()


def hide_release_as_duplicate(release_id: int, reason: str, duplicate_of: int | None):
    """Hide a release because dedup matched it to ``duplicate_of``."""
    conn = get_db()
    conn.execute(
        "UPDATE releases SET is_visible = 0, hidden_reason = ?, "
        "duplicate_of = ?, dedup_ignored = 0 WHERE id = ?",
        (reason, duplicate_of, release_id),
    )
    _maybe_commit()


def restore_dedup_release(release_id: int):
    """Unhide a release that was hidden by dedup (not by the user)."""
    conn = get_db()
    conn.execute(
        "UPDATE releases SET is_visible = 1, hidden_reason = '', "
        "duplicate_of = NULL WHERE id = ?",
        (release_id,),
    )
    _maybe_commit()


# Source preference when choosing which copy of a duplicate survives.
# MusicBrainz is authoritative; iTunes and SoundCloud are complementary and
# only fill in releases MB has no entry for (yet).
SOURCE_PRIORITY = {"musicbrainz": 0, "itunes": 1, "soundcloud": 2}


def source_priority(source: str) -> int:
    """Rank a release source; lower wins, unknown sources rank last."""
    return SOURCE_PRIORITY.get((source or "").lower(), len(SOURCE_PRIORITY))


def normalize_release_type(release_type: str) -> str:
    """Normalize a release type's case/whitespace for comparison.

    Sources already store canonical types (Album/EP/Single/Other). Exact-type
    checks use this; duplicate matching goes through ``release_type_family``
    because iTunes and SoundCloud routinely classify EPs as albums.
    """
    return (release_type or "").strip().casefold()


def release_type_family(release_type: str) -> str:
    """Return the type family used to find duplicate releases.

    Album and EP share one family: iTunes and SoundCloud often have no EP
    classification and report EPs as albums, so an MB EP must be able to
    match their copy. Singles and Other stay separate, so a single and an
    album that share a title remain different releases.
    """
    norm = normalize_release_type(release_type)
    return "album" if norm == "ep" else norm


def are_duplicate_types(
    type_a: str, source_a: str, type_b: str, source_b: str
) -> bool:
    """Whether two release types can describe the same duplicate copy.

    An exact type match is always a duplicate. Album vs EP only counts across
    different sources: MusicBrainz can legitimately have both an album and an
    EP with the same title, and those must stay distinct.
    """
    norm_a = normalize_release_type(type_a)
    norm_b = normalize_release_type(type_b)
    if norm_a == norm_b:
        return True
    if norm_a in ("album", "ep") and norm_b in ("album", "ep"):
        return (source_a or "").strip().casefold() != (
            source_b or ""
        ).strip().casefold()
    return False


def hide_lower_priority_title_duplicates(artist_id: int, source: str) -> int:
    """Hide visible lower-priority rows matching a ``source`` release.

    A match requires both the same normalized title *and* the same
    release-type family: Album and EP compare together because iTunes and
    SoundCloud often report EPs as albums, so an MB EP can take over their
    copy. Singles and Other stay distinct: an MB album and an iTunes single
    that happen to share a title are different releases and must both stay
    visible. Used right after importing a source so a newly-arrived
    higher-priority entry takes over immediately instead of waiting for the
    next dedup run (e.g. MB appearing for a release iTunes previously
    covered). Rows the user manually restored (`dedup_ignored`) are never
    hidden here.

    Returns the number of rows hidden.
    """
    rank = source_priority(source)
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, release_type, source, dedup_ignored FROM releases "
        "WHERE artist_id = ? AND is_visible = 1 "
        "ORDER BY first_seen_at, id",
        (artist_id,),
    ).fetchall()

    keepers: dict[tuple[str, str], int] = {}
    for row in rows:
        if (row["source"] or "").lower() != source.lower():
            continue
        norm = normalize_release_title(row["title"])
        if norm:
            key = (norm, release_type_family(row["release_type"]))
            keepers.setdefault(key, row["id"])
    if not keepers:
        return 0

    hidden = 0
    for row in rows:
        if row["dedup_ignored"]:
            continue  # user chose to keep this copy visible
        if source_priority(row["source"]) <= rank:
            continue
        norm = normalize_release_title(row["title"])
        if not norm:
            continue
        key = (norm, release_type_family(row["release_type"]))
        keeper = keepers.get(key)
        if keeper is not None:
            hide_release_as_duplicate(row["id"], HIDE_REASON_DEDUP_TITLE, keeper)
            hidden += 1
    return hidden


def mark_all_releases_seen():
    conn = get_db()
    conn.execute("UPDATE releases SET notified = 1")
    _maybe_commit()



def get_releases_due_today() -> list[dict]:
    """Find releases with today's exact date (YYYY-MM-DD) not yet notified for release day."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_db()
    rows = conn.execute(
        """SELECT r.*, a.name as artist_name
           FROM releases r
           JOIN artists a ON r.artist_id = a.id
           WHERE r.release_date = ? AND r.release_day_notified = 0
             AND r.is_visible = 1""",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_release_day_notified(release_id: int):
    conn = get_db()
    conn.execute("UPDATE releases SET release_day_notified = 1 WHERE id = ?", (release_id,))
    _maybe_commit()


def get_unseen_count() -> int:
    conn = get_db()
    row = conn.execute("SELECT COUNT(*) as cnt FROM releases WHERE notified = 0 AND is_visible = 1").fetchone()
    return row["cnt"]


def get_artist_single_titles(artist_id: int) -> list[str]:
    """Return lowercase titles of all visible Single releases for an artist.

    Hidden copies must not feed the tracklist ``SINGLE`` badge: dedup hides
    SoundCloud album tracks as duplicate Singles, and those hidden rows would
    otherwise badge every album track.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT title FROM releases "
        "WHERE artist_id = ? AND release_type = 'Single' AND is_visible = 1",
        (artist_id,),
    ).fetchall()
    return [row["title"].strip().lower() for row in rows if row["title"]]


def get_artist_id_by_release_mbid(mbid: str) -> int | None:
    """Look up artist_id from a release MBID."""
    conn = get_db()
    row = conn.execute(
        "SELECT artist_id FROM releases WHERE mbid = ?", (mbid,)
    ).fetchone()
    return row["artist_id"] if row else None


def get_release_by_mbid(mbid: str, artist_id: int) -> dict | None:
    """Look up a release by mbid and artist_id."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM releases WHERE mbid = ? AND artist_id = ?",
        (mbid, artist_id),
    ).fetchone()
    return dict(row) if row else None


def get_release_by_id(release_id: str) -> dict | None:
    """Look up a release by its mbid or itunes_collection_id.

    Returns the full release row including artist info, or None.
    """
    conn = get_db()
    row = conn.execute(
        """SELECT r.*, a.name as artist_name, a.mbid as artist_mbid
           FROM releases r
           JOIN artists a ON r.artist_id = a.id
           WHERE r.mbid = ? OR r.itunes_collection_id = ?""",
        (release_id, release_id),
    ).fetchone()
    return dict(row) if row else None


# --- API response cache ---
# Persisted TTL cache for on-demand upstream reads (tracklists, streaming
# links, an artist's MB links). Entries are keyed "<kind>:<args>" and carry an
# ISO fetch time; cache.py owns the TTLs. The table predates this code (a
# previously-removed cache layer used the same key convention), so reusing it
# keeps the schema stable and drops straight onto existing databases.

def cache_get(key: str) -> tuple[str, str] | None:
    """Return (value_json, fetched_at) for a cached entry, or None."""
    conn = get_db()
    row = conn.execute(
        "SELECT value, fetched_at FROM api_cache WHERE key = ?", (key,)
    ).fetchone()
    return (row["value"], row["fetched_at"]) if row else None


def cache_put(key: str, value: str):
    """Store/refresh one cache entry. ``value`` is a JSON string."""
    conn = get_db()
    conn.execute(
        "INSERT INTO api_cache (key, value, fetched_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "fetched_at = excluded.fetched_at",
        (key, value, _now_iso()),
    )
    _maybe_commit()


def cache_prune(max_age_days: int = 90) -> int:
    """Delete cache entries older than ``max_age_days``; returns rows removed."""
    conn = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    cur = conn.execute("DELETE FROM api_cache WHERE fetched_at < ?", (cutoff,))
    _maybe_commit()
    return cur.rowcount or 0


# --- Meta (key/value state) ---

def get_meta(key: str) -> str | None:
    conn = get_db()
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def update_release_track_titles(release_id: int, track_titles: list[str]):
    """Store track titles for an album/EP release (JSON list)."""
    import json
    conn = get_db()
    conn.execute(
        "UPDATE releases SET track_titles = ? WHERE id = ?",
        (json.dumps(track_titles), release_id),
    )
    _maybe_commit()


def update_release_mb_url(release_id: int, mb_url: str):
    """Store a release's source page URL (backfill for rows imported without one)."""
    conn = get_db()
    conn.execute(
        "UPDATE releases SET mb_url = ? WHERE id = ?",
        (mb_url, release_id),
    )
    _maybe_commit()


# --- Dedup title ignore rules ---

DEFAULT_DEDUP_IGNORES: list[str] = [" - Single", " - EP", "(DJ Mix)", "(Clean)"]

DEDUP_IGNORES_MAX_RULES = 50
DEDUP_IGNORES_MAX_LEN = 60


def get_dedup_ignores() -> list[str]:
    """Return user-defined title suffixes ignored for duplicate detection.

    Falls back to DEFAULT_DEDUP_IGNORES when nothing is stored yet.
    """
    import json
    raw = get_meta("setting_dedup_ignores")
    if not raw:
        return list(DEFAULT_DEDUP_IGNORES)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return list(DEFAULT_DEDUP_IGNORES)
    if not isinstance(data, list):
        return list(DEFAULT_DEDUP_IGNORES)
    cleaned = clean_dedup_ignores(data)
    return cleaned or list(DEFAULT_DEDUP_IGNORES)


def clean_dedup_ignores(rules) -> list[str]:
    """Validate/normalize a raw rule list.

    Leading whitespace is significant (' - Single' needs its space), so only
    trailing whitespace is removed. Dedupe is case-insensitive on the
    surrounding-whitespace-stripped form. Rules that are only wildcards
    ('*', ' * ') are rejected since they'd erase every title. No quotes
    needed — rules are matched literally except '*' (wildcard = any text).
    """
    import re
    seen: set[str] = set()
    out: list[str] = []
    if not isinstance(rules, list):
        return out
    for r in rules:
        if not isinstance(r, str):
            continue
        if not r.strip():
            continue
        t = r.rstrip()
        if len(t) > DEDUP_IGNORES_MAX_LEN:
            t = t[:DEDUP_IGNORES_MAX_LEN].rstrip()
            if not t.strip():
                continue
        # Reject wildcard-only rules (would match everything)
        if not re.sub(r'[\*\s]', '', t):
            continue
        key = t.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= DEDUP_IGNORES_MAX_RULES:
            break
    return out


def set_dedup_ignores(rules) -> list[str]:
    """Persist validated ignore rules, returning the cleaned list."""
    import json
    cleaned = clean_dedup_ignores(rules)
    set_meta("setting_dedup_ignores", json.dumps(cleaned))
    return cleaned


def _rule_to_suffix_pattern(rule: str):
    """Compile a rule into an end-anchored regex.

    The rule is literal text except '*' which matches any text (including
    empty). Matching is case-insensitive, e.g. '(feat.*' strips
    '(feat. Artist)' or '(FEAT. X & Y)' off the end of a title.

    A '*' inside properly closed parens matches only within that block
    ('[^)]*'), so '(feat.*)' strips exactly '(feat. Artist)' while an
    unclosed '(feat.*' stays greedy and also eats trailing tags.
    """
    import re
    return re.compile(_rule_to_suffix_body(rule) + r"\s*$", re.IGNORECASE)


def _rule_to_global_pattern(rule: str):
    """Compile a closed-paren rule into a anywhere-matching regex.

    Returns None unless the rule contains properly closed parens, e.g.
    '(feat.*)' or '(Clean)'. Used to remove tag blocks wherever they sit
    in the title — e.g. 'Song (feat. A) F*ck' needs the feat block gone
    from the middle, which end-anchored stripping can never reach.
    """
    import re
    if "(" not in rule or ")" not in rule:
        return None
    stack = 0
    for ch in rule:
        if ch == "(":
            stack += 1
        elif ch == ")":
            stack -= 1
            if stack == 0:
                break
    else:
        return None
    if stack != 0:
        return None
    return re.compile(_rule_to_suffix_body(rule), re.IGNORECASE)


def _rule_to_suffix_body(rule: str) -> str:
    """Shared body compiler: '*' inside closed parens is '[^)]*', else '.*'."""
    import re
    closed: list[tuple[int, int]] = []
    stack: list[int] = []
    for i, ch in enumerate(rule):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            closed.append((stack.pop(), i))

    def _in_closed(pos: int) -> bool:
        return any(s < pos < e for s, e in closed)

    parts = []
    for i, ch in enumerate(rule):
        if ch == "*":
            parts.append("[^)]*" if _in_closed(i) else ".*")
        else:
            parts.append(re.escape(ch))
    return "".join(parts)


def _explicit_to_token_pattern(censored: str):
    """Compile a censored form into a full-token regex.

    '*' matches any text; the whole token must match. Only applied to
    tokens containing a literal '*', so clean titles are never rewritten.
    """
    import re
    parts = censored.split("*")
    body = ".*".join(re.escape(p) for p in parts)
    return re.compile(body + r"\Z", re.IGNORECASE)


# Compiled dedup/explicit rules, rebuilt only when the underlying settings
# change (see _invalidate_rule_cache). Without this, every title
# normalization re-read both settings from SQLite and recompiled every
# rule's regex — the single hottest CPU path during scans.
_compiled_rules: dict | None = None
_rule_version = 0


def _invalidate_rule_cache():
    """Drop compiled rules; called whenever a setting_ meta row is written."""
    global _compiled_rules, _rule_version
    _compiled_rules = None
    _rule_version += 1


def _get_compiled_rules() -> dict:
    """Compile (once) the default dedup/explicit rules from settings."""
    global _compiled_rules
    if _compiled_rules is None:
        ignores = get_dedup_ignores()
        explicit_map = get_explicit_map()
        # Closed-paren tags match anywhere; the rest only match at the end
        global_patterns = []
        suffix_rules = []
        for s in [s for s in ignores if s and s.strip()]:
            gp = _rule_to_global_pattern(s)
            if gp is not None:
                global_patterns.append(gp)
            else:
                suffix_rules.append(s)
        # Longest rules first for stability
        suffix_rules = sorted(suffix_rules, key=len, reverse=True)
        _compiled_rules = {
            "global": global_patterns,
            "suffix": [(_rule_to_suffix_pattern(s), len(s)) for s in suffix_rules],
            "explicit": sorted(
                ((_explicit_to_token_pattern(c), clean)
                 for c, clean in explicit_map if c and clean),
                key=lambda pc: len(pc[0].pattern),
                reverse=True,
            ),
        }
    return _compiled_rules


@lru_cache(maxsize=65536)
def _normalize_title_cached(title: str, version: int) -> str:
    """Core normalization against the cached rules.

    lru_cache keyed on (title, _rule_version): a settings write bumps the
    version, so stale entries can never be served after a rule change.
    Scans re-check the same thousands of titles every pass, so the hit
    rate is what makes repeated scans cheap.
    """
    rules = _compiled_rules if _compiled_rules is not None else _get_compiled_rules()
    return _apply_rules(title, rules)


def _apply_rules(title: str, rules: dict) -> str:
    """Apply precompiled rules to one title (steps 1-4 of the docstring)."""
    text = (title or "").strip()
    token_patterns = rules["explicit"]
    if token_patterns and "*" in text:
        words = []
        for token in text.split():
            if "*" in token:
                for pattern, clean in token_patterns:
                    if pattern.match(token):
                        token = clean
                        break
            words.append(token)
        text = " ".join(words)
    for pattern in rules["global"]:
        new_text = pattern.sub("", text)
        if new_text != text:
            text = re.sub(r"\s+", " ", new_text).strip()
    # Cap passes to avoid pathological loops
    for _ in range(20):
        stripped_any = False
        for pattern, _size in rules["suffix"]:
            new_text = pattern.sub("", text).strip()
            if new_text != text:
                text = new_text
                stripped_any = True
                break
        if not stripped_any or not text:
            break
    text = text.lower().strip()
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text


def normalize_release_title(title: str, ignores: list[str] | None = None,
                              explicit_map: list | None = None) -> str:
    """Normalize a release/track title for duplicate detection.

    1. Rewrites censored tokens (containing '*') to their clean form via
       the explicit-word map — clean tokens are never touched. This runs
       first so greedy suffix patterns (e.g. '(feat.*') see resolved text
       instead of swallowing explicit words.
    2. Removes closed-paren tag blocks (e.g. '(feat.*)', '(Clean)')
       wherever they sit in the title — mid-title blocks are unreachable
       for end-anchored stripping.
    3. Strips each remaining rule from the end of the title
       (case-insensitive, repeatedly; unclosed '*' stays greedy).
    4. Lowercases, strips punctuation and collapses whitespace.

    With the default rules (the app's only call pattern) results are
    cached per (title, settings version). Explicit ignores/explicit_map
    arguments compile fresh rules per call and stay uncached.
    """
    if ignores is None and explicit_map is None:
        return _normalize_title_cached(title or "", _rule_version)
    if ignores is None:
        ignores = get_dedup_ignores()
    if explicit_map is None:
        explicit_map = get_explicit_map()
    active = [s for s in ignores if s and s.strip()]
    global_patterns = []
    suffix_rules = []
    for s in active:
        gp = _rule_to_global_pattern(s)
        if gp is not None:
            global_patterns.append(gp)
        else:
            suffix_rules.append(s)
    suffix_rules = sorted(suffix_rules, key=len, reverse=True)
    rules = {
        "global": global_patterns,
        "suffix": [(_rule_to_suffix_pattern(s), len(s)) for s in suffix_rules],
        "explicit": sorted(
            ((_explicit_to_token_pattern(c), clean)
             for c, clean in explicit_map if c and clean),
            key=lambda pc: len(pc[0].pattern),
            reverse=True,
        ),
    }
    return _apply_rules(title, rules)


# --- Explicit-word mappings (censored -> clean) ---

DEFAULT_EXPLICIT_MAP: list[list[str]] = [
    ["sh*t", "shit"],
    ["s**t", "shit"],
    ["f*ck", "fuck"],
    ["f**k", "fuck"],
    ["f*cking", "fucking"],
    ["sh*tting", "shitting"],
    ["b*tch", "bitch"],
    ["b****", "bitch"],
    ["p*ssy", "pussy"],
    ["d*ck", "dick"],
    ["c*nt", "cunt"],
    ["wh*re", "whore"],
    ["sl*t", "slut"],
    ["a**", "ass"],
    ["d*mn", "damn"],
]

EXPLICIT_MAP_MAX_PAIRS = 100
EXPLICIT_MAP_MAX_LEN = 60


def get_explicit_map() -> list[list[str]]:
    """Return user-defined [censored, clean] pairs for duplicate detection.

    Falls back to DEFAULT_EXPLICIT_MAP when nothing is stored yet.
    """
    import json
    raw = get_meta("setting_explicit_map")
    if not raw:
        return [list(p) for p in DEFAULT_EXPLICIT_MAP]
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [list(p) for p in DEFAULT_EXPLICIT_MAP]
    if not isinstance(data, list):
        return [list(p) for p in DEFAULT_EXPLICIT_MAP]
    cleaned = clean_explicit_map(data)
    return cleaned or [list(p) for p in DEFAULT_EXPLICIT_MAP]


def clean_explicit_map(pairs) -> list[list[str]]:
    """Validate/normalize raw mapping pairs.

    Rules: exactly [censored, clean] strings; censored must contain '*'
    (otherwise the mapping could never trigger safely); wildcard-only
    censored forms ('*', ' * ') are rejected since they'd match everything;
    clean must not contain '*'. Dedupe case-insensitively on the censored side.
    """
    import re
    seen: set[str] = set()
    out: list[list[str]] = []
    if not isinstance(pairs, list):
        return out
    for entry in pairs:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        censored, clean = entry
        if not isinstance(censored, str) or not isinstance(clean, str):
            continue
        censored = censored.strip()
        clean = clean.strip()
        if not censored or not clean:
            continue
        if "*" not in censored or "*" in clean:
            continue
        if not re.sub(r'[\*\s]', '', censored):
            continue
        if len(censored) > EXPLICIT_MAP_MAX_LEN:
            continue
        if len(clean) > EXPLICIT_MAP_MAX_LEN:
            continue
        key = censored.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append([censored, clean])
        if len(out) >= EXPLICIT_MAP_MAX_PAIRS:
            break
    return out


def set_explicit_map(pairs) -> list[list[str]]:
    """Persist validated mappings, returning the cleaned list."""
    import json
    cleaned = clean_explicit_map(pairs)
    set_meta("setting_explicit_map", json.dumps(cleaned))
    return cleaned


# Settings are read on hot paths (per artist during scans, per search) but
# written rarely, so the full dict is cached until the next write.
_settings_cache: dict[str, str] | None = None


def set_meta(key: str, value: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    if key.startswith("setting_"):
        _invalidate_settings_cache()
    _maybe_commit()


def get_all_settings() -> dict[str, str]:
    """Return all settings as a dict. Settings are stored in the meta table
    with keys prefixed by 'setting_'."""
    global _settings_cache
    if _settings_cache is None:
        conn = get_db()
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'setting_%'"
        ).fetchall()
        # Strip the 'setting_' prefix for the returned keys
        _settings_cache = {row["key"][8:]: row["value"] for row in rows}
    return dict(_settings_cache)


def _invalidate_settings_cache():
    global _settings_cache
    _settings_cache = None
    _invalidate_rule_cache()


def set_setting(key: str, value: str):
    """Save a setting (stored in meta with 'setting_' prefix)."""
    set_meta(f"setting_{key}", value)
