from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent / "music-release-tracker.db")

_conn: sqlite3.Connection | None = None


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
    conn.commit()
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
    conn.commit()


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
    conn.commit()
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
    conn.commit()
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
    conn.commit()
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
    conn.commit()
    row = conn.execute("SELECT * FROM artists WHERE id = ?", (artist_id,)).fetchone()
    return dict(row)


def update_artist_disambiguation(artist_id: int, disambiguation: str) -> dict | None:
    """Set a tracked artist's disambiguation note (free text, may be empty)."""
    conn = get_db()
    conn.execute(
        "UPDATE artists SET disambiguation = ? WHERE id = ?",
        ((disambiguation or "").strip()[:200], artist_id),
    )
    conn.commit()
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
) -> bool:
    """Insert a release, or update release_date if it already exists.
    
    Returns True if inserted or updated, False if unchanged.
    Updates ensure bootleg/unofficial dates get replaced with official dates
    on the next sync.

    credits: JSON string of credited artists (from MusicBrainz artist-credit).
    Existing rows are silently backfilled when credit data arrives or changes —
    this does NOT count as a new release.
    """
    conn = get_db()
    # For non-MB releases, use their own ID as uniqueness key
    if source == "itunes":
        unique_key = itunes_collection_id or mbid
    elif source == "soundcloud":
        unique_key = soundcloud_track_id or mbid
    else:
        unique_key = mbid

    # Build the uniqueness query based on available identifiers
    if soundcloud_track_id:
        existing = conn.execute(
            "SELECT id, release_date, credits FROM releases WHERE artist_id = ? AND soundcloud_track_id = ?",
            (artist_id, soundcloud_track_id),
        ).fetchone()
    elif itunes_collection_id:
        existing = conn.execute(
            "SELECT id, release_date, credits FROM releases WHERE artist_id = ? AND (mbid = ? OR itunes_collection_id = ?)",
            (artist_id, unique_key, unique_key),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT id, release_date, credits FROM releases WHERE artist_id = ? AND mbid = ?",
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
        conn.commit()
        return changed  # True only for inserts / date updates

    conn.execute(
        """INSERT INTO releases
           (mbid, artist_id, source, title, release_type, release_date, first_seen_at, notified, mb_url, itunes_collection_id, artwork_url, credits, soundcloud_track_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (unique_key, artist_id, source, title, release_type, release_date, _now_iso(), notified, mb_url, itunes_collection_id, artwork_url, credits, soundcloud_track_id),
    )
    conn.commit()
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
        SELECT r.*, a.name as artist_name, a.mbid as artist_mbid
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
    conn.commit()


def set_release_visible(release_id: int, visible: bool):
    """Show or hide a release in the feed."""
    conn = get_db()
    conn.execute("UPDATE releases SET is_visible = ? WHERE id = ?", (1 if visible else 0, release_id))
    conn.commit()


def mark_all_releases_seen():
    conn = get_db()
    conn.execute("UPDATE releases SET notified = 1")
    conn.commit()



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
    conn.commit()


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
    conn.commit()


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
    """
    import re
    if ignores is None:
        ignores = get_dedup_ignores()
    text = (title or "").strip()
    if explicit_map is None:
        explicit_map = get_explicit_map()
    if explicit_map and "*" in text:
        token_patterns = sorted(
            ((_explicit_to_token_pattern(c), clean)
             for c, clean in explicit_map if c and clean),
            key=lambda pc: len(pc[0].pattern),
            reverse=True,
        )
        words = []
        for token in text.split():
            if "*" in token:
                for pattern, clean in token_patterns:
                    if pattern.match(token):
                        token = clean
                        break
            words.append(token)
        text = " ".join(words)
    active = [s for s in ignores if s and s.strip()]
    # Closed-paren tags match anywhere; the rest only match at the end
    global_patterns = []
    suffix_rules = []
    for s in active:
        gp = _rule_to_global_pattern(s)
        if gp is not None:
            global_patterns.append(gp)
        else:
            suffix_rules.append(s)
    for pattern in global_patterns:
        new_text = pattern.sub("", text)
        if new_text != text:
            text = re.sub(r"\s+", " ", new_text).strip()
    # Longest rules first for stability; cap passes to avoid pathological loops
    suffix_rules = sorted(suffix_rules, key=len, reverse=True)
    patterns = [(_rule_to_suffix_pattern(s), len(s)) for s in suffix_rules]
    for _ in range(20):
        stripped_any = False
        for pattern, _size in patterns:
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


def set_meta(key: str, value: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_all_settings() -> dict[str, str]:
    """Return all settings as a dict. Settings are stored in the meta table
    with keys prefixed by 'setting_'."""
    conn = get_db()
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'setting_%'"
    ).fetchall()
    # Strip the 'setting_' prefix for the returned keys
    return {row["key"][8:]: row["value"] for row in rows}


def set_setting(key: str, value: str):
    """Save a setting (stored in meta with 'setting_' prefix)."""
    set_meta(f"setting_{key}", value)
