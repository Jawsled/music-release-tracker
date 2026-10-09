from __future__ import annotations

import asyncio
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent / "music-release-tracker.db")

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


def get_counts() -> dict[str, int]:
    """Return artist/release totals (for startup diagnostics)."""
    conn = get_db()
    artists = conn.execute("SELECT COUNT(*) c FROM artists").fetchone()["c"]
    releases = conn.execute("SELECT COUNT(*) c FROM releases").fetchone()["c"]
    return {"artists": artists, "releases": releases}


def backup_database(keep: int = 5) -> str | None:
    """Save a timestamped copy of the database into BACKUP/, pruning old ones.

    Never raises: returns the backup path, or None when anything goes wrong.
    Callers must ensure no scan is mid-write; at startup (right after
    init_db) the connection is idle, which is the safe moment.
    """
    import shutil
    from datetime import datetime
    try:
        backup_dir = Path(DB_PATH).parent / "BACKUP"
        backup_dir.mkdir(exist_ok=True)
        try:
            get_db().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = backup_dir / f"music-release-tracker-{stamp}.db"
        shutil.copy(DB_PATH, dest)
        existing = sorted(backup_dir.glob("music-release-tracker-*.db"))
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
) -> bool:
    """Insert a release, or update release_date if it already exists.
    
    Returns True if inserted or updated, False if unchanged.
    Updates ensure bootleg/unofficial dates get replaced with official dates
    on the next sync.

    credits: JSON string of credited artists (from MusicBrainz artist-credit).
    Existing rows are silently backfilled when credit data arrives or changes —
    this does NOT count as a new release. Missing/changed mb_url is backfilled
    the same way, so rows imported before a source stored URLs still get a
    working link on the next sync.
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
            "SELECT id, release_date, credits, mb_url FROM releases WHERE artist_id = ? AND soundcloud_playlist_id = ?",
            (artist_id, soundcloud_playlist_id),
        ).fetchone()
    elif soundcloud_track_id:
        existing = conn.execute(
            "SELECT id, release_date, credits, mb_url FROM releases WHERE artist_id = ? AND soundcloud_track_id = ?",
            (artist_id, soundcloud_track_id),
        ).fetchone()
    elif itunes_collection_id:
        existing = conn.execute(
            "SELECT id, release_date, credits, mb_url FROM releases WHERE artist_id = ? AND (mbid = ? OR itunes_collection_id = ?)",
            (artist_id, unique_key, unique_key),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT id, release_date, credits, mb_url FROM releases WHERE artist_id = ? AND mbid = ?",
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
        _maybe_commit()
        return changed  # True only for inserts / date updates

    conn.execute(
        """INSERT INTO releases
           (mbid, artist_id, source, title, release_type, release_date, first_seen_at, notified, mb_url, itunes_collection_id, artwork_url, credits, soundcloud_track_id, soundcloud_playlist_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (unique_key, artist_id, source, title, release_type, release_date, _now_iso(), notified, mb_url, itunes_collection_id, artwork_url, credits, soundcloud_track_id, soundcloud_playlist_id),
    )
    _maybe_commit()
    return True


def get_releases(
    artist_id: int | None = None,
    release_type: list[str] | None = None,
    unseen_only: bool = False,
    include_hidden: bool = False,
    hidden_only: bool = False,
) -> list[dict]:
    conn = get_db()
    query = """
        SELECT r.*, a.name as artist_name, a.mbid as artist_mbid,
               a.soundcloud_permalink as artist_soundcloud_permalink
        FROM releases r
        JOIN artists a ON r.artist_id = a.id
        WHERE 1=1
    """
    params: list = []

    if hidden_only:
        query += " AND r.is_visible = 0"
    elif not include_hidden:
        query += " AND r.is_visible = 1"
    if artist_id is not None:
        query += " AND r.artist_id = ?"
        params.append(artist_id)
    if release_type:
        placeholders = ", ".join("?" for _ in release_type)
        query += f" AND r.release_type IN ({placeholders})"
        params.extend(release_type)
    if unseen_only:
        query += " AND r.notified = 0"

    query += " ORDER BY r.release_date DESC, r.first_seen_at DESC"

    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def mark_release_seen(release_id: int):
    """Mark a single release as seen (notified)."""
    conn = get_db()
    conn.execute("UPDATE releases SET notified = 1 WHERE id = ?", (release_id,))
    _maybe_commit()


def set_release_visible(release_id: int, visible: bool):
    """Show or hide a release in the feed."""
    conn = get_db()
    conn.execute("UPDATE releases SET is_visible = ? WHERE id = ?", (1 if visible else 0, release_id))
    _maybe_commit()


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
           WHERE r.release_date = ? AND r.release_day_notified = 0""",
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
    """Return lowercase titles of all Single releases for a given artist."""
    conn = get_db()
    rows = conn.execute(
        "SELECT title FROM releases WHERE artist_id = ? AND release_type = 'Single'",
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


def get_artist_all_track_titles(artist_id: int) -> set[str]:
    """Get all normalized track titles from album/EP tracklists for an artist."""
    import json
    conn = get_db()
    rows = conn.execute(
        "SELECT track_titles FROM releases WHERE artist_id = ? AND track_titles != ''",
        (artist_id,),
    ).fetchall()
    titles = set()
    for row in rows:
        try:
            tracks = json.loads(row["track_titles"])
            for t in tracks:
                norm = normalize_release_title(t)
                if norm:
                    titles.add(norm)
        except (json.JSONDecodeError, TypeError):
            continue
    return titles


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
