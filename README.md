# Music Release Tracker

A local-first desktop app that tracks new album and EP releases from artists you follow. Powered by [MusicBrainz](https://musicbrainz.org/), [iTunes](https://music.apple.com) and [SoundCloud](https://soundcloud.com)

![Theme](https://img.shields.io/badge/theme-dark-1a1a2e) ![Python](https://img.shields.io/badge/python-3.8%2B-bb86fc) ![License: MIT](https://img.shields.io/badge/license-MIT-03dac6)

---

## Features

- **Artist search:** Search MusicBrainz, iTunes and SoundCloud by name (platform filter included in the search box), or paste an MBID, a MusicBrainz / Apple Music / SoundCloud URL, or a SoundCloud permalink. Preview buttons and pills let you confirm you're adding the right artist before you commit.
- **SoundCloud suggestions:** SoundCloud has no text search API, so display names are probed as profile URL variants. Each suggestion shows the live follower count, which is usually the fastest way to tell same-name profiles apart. iTunes rows show album counts for the same reason.
- **Release feed:** Browse albums, EPs, singles and other releases sorted by date; filter by artist, release type, new-only, or hidden-only.
- **Tracklists:** Fetch tracklists for album and EP releases, including track numbers, durations and featured-artist credits. Tracks also released as singles get a `SINGLE` badge.
- **Streaming links:** Fetch external streaming links listed on the MusicBrainz release page (Spotify, Apple Music, Tidal, and more).
- **Duplicate handling:** MusicBrainz is the authoritative source: matching iTunes/SoundCloud copies are stored and then hidden by dedup, so those sources only fill in releases MB doesn't cover (yet) and every fetched copy remains recoverable in the Hidden view. Matching compares normalized title **and release type**, so an album, EP and single with the same name all stay visible; MusicBrainz copies of the same type are collapsed to one. User-defined title ignore rules (triggered by the Dedup task) strip noise like `- Single`, `- EP`, `(DJ Mix)` or `(feat. ...)` suffixes before comparing titles. Explicit-word mappings reconcile censored Apple Music titles. Dedup records why it hid a release, so manual hides/restores are respected and removing a rule restores releases that are no longer duplicates.
- **New release detection:** "Check All Artists" scans every tracked artist (or just the one selected in the feed filter) and highlights new finds with a `NEW` badge. Scans can be paused/resumed, play a chime on completion, and log everything to the Scan Logs panel.
- **Sources:** Each data source can be toggled in Settings. Disabled sources are skipped when scanning and hidden across search, artist icons and link options. Existing releases are never touched by toggling.
- **Hide / unhide:** Hide individual releases from the feed (with undo), or review everything hidden via the Hidden only filter.
- **Artist notes:** The 3-dot menu on each tracked artist lets you edit a free-text note, check a single artist, link or unlink sources, and untrack.
- **Logs:** The Scan Logs panel streams scan activity with level filters, text search and pause.
- **Startup options:** Optionally scan for new releases and/or run dedup automatically when the app starts. A one-time dedup repair also runs after a dedup-policy upgrade, regardless of this setting.
- **Easy backup:** Import/export your artists as a `.json` file, or stop the app and copy `music-release-tracker.db` from the app folder. That single file is self-contained (it holds artists, releases and settings), so no sidecar files need to come along — but the database runs in WAL mode, so let the app shut down cleanly first. **Never copy the `.db` while the app is running:** without the `-wal` sidecar the copy is missing recent writes, and a copy taken mid-write can be corrupt. After a force-kill, recent writes can still be sitting in `music-release-tracker.db-wal`; either copy it alongside the `.db` or let the app start and stop once so the WAL is checkpointed. The app also writes a consistent database snapshot into `BACKUP/` on every startup (using SQLite's online backup API; latest five kept) and a JSON artist snapshot before schema migrations plus once a day while running (latest 30 kept).

## Quick Start

```bash
git clone https://github.com/Jawsled/music-release-tracker.git
cd music-release-tracker
python run.py
```

On Windows you can instead run `Music-Release-Tracker.bat`. To add it to the Start Menu, create a shortcut to the `.bat` file and place it in:

```
C:\ProgramData\Microsoft\Windows\Start Menu\Programs
```

That's it. On first run the app creates a dedicated launcher environment, installs dependencies automatically, then opens the web UI at `http://127.0.0.1:7070`.

### Conda (optional)

A `Music-Release-Tracker (conda).bat` launcher is included if you prefer running under Anaconda for whatever reason. The project already makes its own venv, but the option is there. The conda environment name should be `music-release-tracker`.

## General Workflow

1. **Add artists:** Search by name and pick from results, or paste a URL if search doesn't find who you're looking for. Merged rows can link all available platforms in one click.
2. **Check for new releases:** The check respects the artist selected in the feed's dropdown: with *all artists* shown it scans everyone; with a specific artist selected it only checks that one (faster, and avoids rate limiting). Results can be jumped to straight from the summary.
3. **Browse your feed:** Each release shows title, artist, type, date, source icon, and cover art. New releases carry a `NEW` badge. Click it to dismiss, or use *Mark All as Seen*. Expand cards for tracklists and streaming links.

> When you first add an artist, all of their existing releases are imported as *already seen*, so your feed isn't flooded. Only releases found by later checks are marked new.

## Pages

### Artists

- **Search:** Queries the MusicBrainz, iTunes and SoundCloud APIs (whichever platforms are selected and enabled). Names matching across platforms merge into one row so you can link everything at once.
- **Manual query:** If search comes up empty, paste an MBID, a full MusicBrainz / Apple Music / SoundCloud URL, or a SoundCloud permalink.
- **Linking:** Already have an artist added via one source? Link the other sources to the same entry from the 3-dot menu. Unlinking asks for confirmation inline, then removes that source's release entries.
- **Import / Export:** Export/import artist entries (name, MBIDs, iTunes IDs, SoundCloud permalinks) as JSON. The app also writes the same format to `BACKUP/` automatically.
- **Sorting:** The tracked list sorts alphabetically, case-insensitively, with accented characters folded to their base letters.

### Feed

- **Artist dropdown:** Choose whose releases to view (searchable, keyboard navigable).
- **Check for new releases:** Checks the artist(s) currently selected in the dropdown (all artists by default). Progress, per-artist failures with retry, and a new-release summary appear in the popup.
- **Type filters:** Release-type chips show/hide releases per type and combine with the other filters.
  - *MusicBrainz types:* Everything classified as a release is fetched (albums, EPs, singles, and various mixes/compilations). Anything that isn't an album, EP, or single falls under **Other** when filtering. The exact type still appears on each release card.
  - *iTunes types:* Since iTunes labels everything `collectionType=Album`, classification uses title suffixes like `- Single` / `- EP`; anything else counts as an album.
- **Duplicate handling:** Scans store every release each enabled source returns, then run dedup afterwards; duplicates are registered and hidden instead of being skipped at fetch time. Title ignore rules plus explicit-word mappings (both editable in Settings) decide what counts as the same release. A duplicate must match the normalized title **and** the release type (album/EP/single/other). MusicBrainz wins over iTunes, which wins over SoundCloud; every copy in a matching group except the winner is hidden. Run the rules over your library any time with Run Dedup Now. Dedup only hides copies it hid itself — manual hides and manually restored releases are left alone — and it restores releases that stop matching after a rule change. A manually hidden row stays hidden on later scans and dedup runs; if it was the visible winner, the best remaining copy can fill in. Unlinking MB brings the complementary iTunes/SoundCloud copy back.
- **New-only checkbox:** Shows only entries marked `NEW`. "New" here means newly added to the database. This has nothing to do with release dates. Dismiss badges individually or via *Mark All as Seen*.
- **Hidden only checkbox:** Shows everything hidden — manual hides plus copies dedup set aside — each with an Unhide action. Unhiding a dedup copy also marks it as user-preferred so a later dedup pass leaves it visible.
- **Tracklist:** Fetched from MusicBrainz/iTunes/SoundCloud for anything other than singles, with a copy button.
- **`SINGLE` badge:** Purely local: matches track titles against the artist's single releases.

## Database

SQLite database (`music-release-tracker.db`) with four active tables. Older databases may also contain an unused legacy `release_artists` table.

| Table | Columns |
| --- | --- |
| `artists` | internal ID, `mbid`, `name`, `disambiguation`, `itunes_artist_id`, `soundcloud_permalink`, `added_at` |
| `releases` | internal ID, `mbid` (iTunes/SoundCloud IDs also live here), artist ID, `source` (`musicbrainz`/`itunes`/`soundcloud`), `title`, `release_type`, `release_date`, `first_seen_at`, `notified`, `release_day_notified` *(reserved, currently unused)*, `mb_url`, `itunes_collection_id`, `soundcloud_track_id`, `soundcloud_playlist_id`, `artwork_url`, `credits`, `track_titles`, `is_visible`, `hidden_reason`, `duplicate_of`, `dedup_ignored` |
| `meta` | key/value store for settings (keys prefixed with `setting_`) and internal state |
| `api_cache` | upstream response cache: `key` (`<kind>:<id>`), `value` (JSON), `fetched_at` |

`init_db()` upgrades existing database files in place on startup: missing tables, columns and indexes are added automatically, and schema changes are additive, so old databases keep working without a migration step. Indexes cover artist lookups by source ID, visible/unseen feed queries, and dedup's hidden-reason scans. In `releases`, `hidden_reason` records `manual`, `dedup_title` or `dedup_track`; `duplicate_of` points at the surviving copy; and `dedup_ignored` marks a copy you explicitly restored, which dedup will leave visible.

Alongside the database snapshots, the tracked artist list is written to `BACKUP/music-release-tracker-artists-<timestamp>.json` before migrations run at startup and every 24 hours while the app stays open. These files use the same format as the Artists page export and can be imported directly; the latest 30 are kept.

Cover art for MusicBrainz releases is proxied through `/api/covers/{mbid}` and cached on disk in `.cache/covers/` (regenerable — safe to delete). The feed loads one page at a time and appends more as you scroll. Tracklists, streaming links, and an artist's MusicBrainz links are cached in `api_cache` (TTLs in `cache.py`; entries untouched for 90 days are pruned at startup).

## Known Issues

- **iTunes quirks:** iTunes provides no release type (everything is `collectionType=Album`), so suffix-based classification is used. Fixing this properly would require the modern Apple Music API key. Also the iTunes catalog is less frequently maintained. Some releases only surface via UPC lookup. iTunes also exposes no follower counts, so search rows show album counts instead to help tell same-name artists apart.
- **Near-duplicate titles:** Deduplication can still miss releases whose titles differ in ways no rule covers. Fully censored words (`****`) cannot be mapped to anything, by design.
- **MusicBrainz credits:** The scan works at the *release group* level (individual releases are only fetched on demand for tracklists/streaming links), so credited artists don't appear during a release-group scan.

### Telegram (removed)

Telegram support was dropped during a significant rewrite. Feel free to build your own Telegram/Discord/etc. integrations on top.

## Tech Stack

| Layer | Tech |
| --- | --- |
| Backend | Python, FastAPI, SQLite |
| Frontend | Vanilla HTML/CSS/JS (dark theme) |
| Data sources | [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API), [iTunes Search API](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI), [SoundCloud V2 API](https://soundcloud.com), no API keys required |

## Requirements

- Python 3.8+
- Internet connection (for MusicBrainz / iTunes / SoundCloud APIs)

The original launcher installs dependencies automatically into its dedicated `.venv-run/` environment from [`requirements.txt`](requirements.txt). If you use uv, the project environment is kept separately in `.venv/`:

```bash
uv sync
uv run music-release-tracker
```

Do not point `run.py` at uv's `.venv`; the two launch methods intentionally use separate environments.

## Acknowledgements

Icons by [thesvg.org](https://thesvg.org), [simpleicons.org](https://simpleicons.org), and [tabler.io](https://tabler.io).

## Disclosure on LLM Use

- The original release by Katemptiness was co-authored with Claude.
- This fork contains bugfixes and feature additions assisted by Qwen3.6 & Qwen3.8
