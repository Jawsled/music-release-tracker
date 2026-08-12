from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

DB_PATH = "data/releases.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
    conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Artists ---

def add_artist(mbid: str, name: str, disambiguation: str = "", itunes_artist_id: int | None = None) -> dict:
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO artists (mbid, name, disambiguation, itunes_artist_id, added_at) VALUES (?, ?, ?, ?, ?)",
            (mbid, name, disambiguation, itunes_artist_id, _now_iso()),
        )
        conn.commit()
        # For iTunes artists (empty mbid), look up by itunes_artist_id
        if itunes_artist_id:
            row = conn.execute("SELECT * FROM artists WHERE itunes_artist_id = ?", (itunes_artist_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM artists WHERE mbid = ?", (mbid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def remove_artist(artist_id: int):
    conn = get_db()
    try:
        conn.execute("DELETE FROM artists WHERE id = ?", (artist_id,))
        conn.commit()
    finally:
        conn.close()


def get_all_artists() -> list[dict]:
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM artists ORDER BY name").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_artist_by_mbid(mbid: str) -> dict | None:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM artists WHERE mbid = ?", (mbid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_artist_by_itunes_id(itunes_artist_id: int) -> dict | None:
    """Look up an artist by their iTunes artist ID."""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM artists WHERE itunes_artist_id = ?", (itunes_artist_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def link_artist(artist_id: int, mbid: str, itunes_artist_id: int | None) -> dict:
    """Link a MusicBrainz or iTunes ID to an existing artist entry."""
    conn = get_db()
    try:
        # Update only non-empty/non-None values
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
        conn.commit()
        row = conn.execute("SELECT * FROM artists WHERE id = ?", (artist_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def unlink_artist_itunes(artist_id: int) -> dict:
    """Remove iTunes ID from an artist entry."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE artists SET itunes_artist_id = NULL WHERE id = ?",
            (artist_id,),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM artists WHERE id = ?", (artist_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def unlink_artist_mb(artist_id: int) -> dict:
    """Remove MusicBrainz ID from an artist entry."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE artists SET mbid = NULL WHERE id = ?",
            (artist_id,),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM artists WHERE id = ?", (artist_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


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
) -> bool:
    """Insert a release, or update release_date if it already exists.
    
    Returns True if inserted or updated, False if unchanged.
    Updates ensure bootleg/unofficial dates get replaced with official dates
    on the next sync.
    """
    conn = get_db()
    try:
        # For iTunes releases, use collectionId as mbid for uniqueness
        unique_key = mbid if source == "musicbrainz" else (itunes_collection_id or mbid)
        
        existing = conn.execute(
            "SELECT id, release_date FROM releases WHERE artist_id = ? AND (mbid = ? OR itunes_collection_id = ?)",
            (artist_id, unique_key, unique_key),
        ).fetchone()
        
        if existing:
            # Update release_date if it differs (e.g., bootleg date -> official date)
            if existing["release_date"] != release_date:
                conn.execute(
                    "UPDATE releases SET release_date = ? WHERE id = ?",
                    (release_date, existing["id"]),
                )
                conn.commit()
                return True  # Updated
            return False  # Unchanged

        conn.execute(
            """INSERT INTO releases
               (mbid, artist_id, source, title, release_type, release_date, first_seen_at, notified, mb_url, itunes_collection_id, artwork_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (unique_key, artist_id, source, title, release_type, release_date, _now_iso(), notified, mb_url, itunes_collection_id, artwork_url),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_releases(
    artist_id: int | None = None,
    release_type: list[str] | None = None,
    unseen_only: bool = False,
) -> list[dict]:
    conn = get_db()
    try:
        query = """
            SELECT r.*, a.name as artist_name, a.mbid as artist_mbid
            FROM releases r
            JOIN artists a ON r.artist_id = a.id
            WHERE 1=1
        """
        params: list = []

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
    finally:
        conn.close()


def mark_release_seen(release_id: int):
    """Mark a single release as seen (notified)."""
    conn = get_db()
    try:
        conn.execute("UPDATE releases SET notified = 1 WHERE id = ?", (release_id,))
        conn.commit()
    finally:
        conn.close()


def mark_all_releases_seen():
    conn = get_db()
    try:
        conn.execute("UPDATE releases SET notified = 1")
        conn.commit()
    finally:
        conn.close()



def get_releases_due_today() -> list[dict]:
    """Find releases with today's exact date (YYYY-MM-DD) not yet notified for release day."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT r.*, a.name as artist_name
               FROM releases r
               JOIN artists a ON r.artist_id = a.id
               WHERE r.release_date = ? AND r.release_day_notified = 0""",
            (today,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_release_day_notified(release_id: int):
    conn = get_db()
    try:
        conn.execute("UPDATE releases SET release_day_notified = 1 WHERE id = ?", (release_id,))
        conn.commit()
    finally:
        conn.close()


def get_unseen_count() -> int:
    conn = get_db()
    try:
        row = conn.execute("SELECT COUNT(*) as cnt FROM releases WHERE notified = 0").fetchone()
        return row["cnt"]
    finally:
        conn.close()


def get_artist_single_titles(artist_id: int) -> list[str]:
    """Return lowercase titles of all Single releases for a given artist."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT title FROM releases WHERE artist_id = ? AND release_type = 'Single'",
            (artist_id,),
        ).fetchall()
        return [row["title"].strip().lower() for row in rows if row["title"]]
    finally:
        conn.close()


def get_artist_id_by_release_mbid(mbid: str) -> int | None:
    """Look up artist_id from a release MBID."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT artist_id FROM releases WHERE mbid = ?", (mbid,)
        ).fetchone()
        return row["artist_id"] if row else None
    finally:
        conn.close()


def get_release_by_id(release_id: str) -> dict | None:
    """Look up a release by its mbid or itunes_collection_id.

    Returns the full release row including artist info, or None.
    """
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT r.*, a.name as artist_name, a.mbid as artist_mbid
               FROM releases r
               JOIN artists a ON r.artist_id = a.id
               WHERE r.mbid = ? OR r.itunes_collection_id = ?""",
            (release_id, release_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# --- Meta (key/value state) ---

def get_meta(key: str) -> str | None:
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_meta(key: str, value: str):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()
