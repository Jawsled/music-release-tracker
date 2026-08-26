# Music Release Tracker

A local-first desktop app that tracks new album and EP releases from artists you follow. Powered by [MusicBrainz](https://musicbrainz.org/) and [iTunes](https://music.apple.com).

![Theme](https://img.shields.io/badge/theme-dark-1a1a2e) ![Python](https://img.shields.io/badge/python-3.8%2B-bb86fc) ![License: MIT](https://img.shields.io/badge/license-MIT-03dac6)

---

## Features

- **Artist search:** Search MusicBrainz and iTunes by name, or paste an MBID, MusicBrainz artist URL, or Apple Music artist URL manually. Preview buttons let you confirm you're adding the right artist.
- **Release feed:** Browse albums, EPs, and singles sorted by date; filter by artist, release type, or new-only.
- **Tracklists:** Fetch tracklists for album and EP releases, including track numbers and durations.
- **Streaming links:** Fetch external streaming links listed on the MusicBrainz release page.
- **Single detection:** When fetching a tracklist, songs that also exist as standalone singles are marked with a `SINGLE` badge — handy for avoiding duplicates.
- **Duplicate handling:** Duplicate releases between MusicBrainz and iTunes are deduplicated, with MB results prioritized. iTunes acts as a complement to catch releases not (yet) on MusicBrainz in near real time.
- **New release detection:** "Check Now" scans all tracked artists and highlights newly discovered releases with a `NEW` badge.
- **Official releases:** Everything from the artist's MusicBrainz *releases* tab is fetched: regular releases plus pre-releases, remixes, DJ sets, compilations, and more.
- **Quick links:** Click `view ↗` on a card to open the release page on MusicBrainz or Apple Music, or click the artist name for their page.
- **Easy backup:** Import/export your artists as a `.json` file (name, MBID, iTunes ID), or copy the `.db` file in the root directory, which also includes scanned releases.

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

That's it. On first run the app creates a virtual environment, installs dependencies automatically, then opens the web UI at `http://127.0.0.1:7070`.

### Conda (optional)

A `Music-Release-Tracker (conda).bat` launcher is included if you prefer running under Anaconda. The conda environment name should be `music-release-tracker`.

## General Workflow

1. **Add artists:** Search by name and pick from results, or paste a URL if search doesn't find who you're looking for.
2. **Check for new releases:** The check respects the artist selected in the feed's dropdown: with *all artists* shown it scans everyone; with a specific artist selected it only checks that one (faster, and avoids rate limiting).
3. **Browse your feed:** Each release shows title, type, date, source (MB/iTunes), and cover art. New releases carry a `NEW` badge — click badges individually to dismiss them, or use *Mark all as seen*. Use your browser's find (`Ctrl+F`) to search for a particular release.

> When you first add an artist, all of their existing releases are imported as *already seen*, so your feed isn't flooded. Only releases found by later checks are marked new.

## Pages

### Artists

- **Search:** Queries the MusicBrainz and iTunes APIs; the MBID / Apple artist ID is extracted and stored in the database.
- **Manual query:** If search comes up empty, paste an MBID, full MusicBrainz artist URL, or Apple Music artist URL.
- **Linking:** Already have an artist added via one source? Manually link the other source to the same entry. Unlinking asks for confirmation, then removes that source's release entries.
- **Import / Export:** Export/import artist entries (name, MBID, iTunes ID) as JSON.

### Feed

- **Artist dropdown:** Choose whose releases to view.
- **Check for new releases:** Checks the artist(s) currently selected in the dropdown (all artists by default).
- **Type filters:** Release-type chips show/hide releases per type and combine with artist filtering.
  - *MusicBrainz types:* Everything classified as a release is fetched (albums, EPs, singles, and various mixes/compilations). Anything that isn't an album, EP, or single falls under **Other** when filtering — the exact type still appears on each release card.
  - *iTunes types:* Since iTunes labels everything `collectionType=Album`, classification uses title suffixes like `- Single` / `- EP`; anything else counts as an album.
- **Duplicate handling:** Rudimentary for now: `- Single`/`- EP` suffixes are stripped from iTunes titles and matched case-insensitively against releases from the same artist.
- **New-only checkbox:** Shows only entries marked `NEW`. "New" here means newly added to the database. This has nothing to do with release dates. Dismiss badges individually or via *Mark all as seen*.
- **Tracklist:** Fetched from MusicBrainz/iTunes for anything other than singles.
- **`SINGLE` badge:** Purely local: matches track titles against the artist's single releases.

## Database

SQLite database (`music-release-tracker.db`) with two tables:

| Table | Columns |
| --- | --- |
| `artists` | internal ID · `mbid` · `name` · `disambiguation` · `itunes_artist_id` · `added_at` |
| `feed` | internal ID · `mbid` (iTunes ID also lives here) · artist ID · `source` (`mb`/`itunes`) · `release_type` · `release_date` · `first_seen_at` · `notified` · `release_day_notified` *(unused)* · `mb_url` · `itunes_collection_id` · `artwork_url` · `external_urls` |

## Known Issues

- **iTunes quirks:** iTunes provides no release type (everything is `collectionType=Album`), so suffix-based classification is used. Fixing this properly would require the modern Apple Music API key, and the iTunes catalog is less frequently maintained — some releases only surface via UPC lookup.
- **Near-duplicate titles:** Deduplication can miss releases whose titles differ slightly. A blanket strip of `(feat. …)` isn't safe, since some tracks gain additional featured artists and legitimately become new releases.
- **MusicBrainz credits:** The scan works at the *release group* level (individual releases are only fetched on demand for tracklists/streaming links), so credited artists don't appear during a release-group scan.

### Telegram (removed)

Telegram support was dropped during a significant rewrite. Feel free to build your own Telegram/Discord/etc. integrations on top.

## Tech Stack

| Layer | Tech |
| --- | --- |
| Backend | Python · FastAPI · SQLite |
| Frontend | Vanilla HTML/CSS/JS (dark theme) |
| Data sources | [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API) · [iTunes Search API](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI) — no API keys required |

## Requirements

- Python 3.8+
- Internet connection (for MusicBrainz / iTunes APIs)

All dependencies are installed automatically by `run.py` (see [`requirements.txt`](requirements.txt)).

## Acknowledgements

Icons by [thesvg.org](https://thesvg.org), [simpleicons.org](https://simpleicons.org), and [tabler.io](https://tabler.io).

## Disclosure on LLM Use

- The original release by Katemptiness was co-authored with Claude.
- This fork contains bugfixes and feature additions assisted by Qwen3.6.
