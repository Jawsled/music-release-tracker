# Music Release Tracker

A local-first desktop app that tracks new album and EP releases from artists you follow, powered by [MusicBrainz](https://musicbrainz.org/).

![Dark themed UI with three tabs: Feed, Artists, and Check](https://img.shields.io/badge/theme-dark-1a1a2e) ![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-bb86fc) ![License: MIT](https://img.shields.io/badge/license-MIT-03dac6)

## Features

- **Artist search** — find and add artists from MusicBrainz with disambiguation (Short descriptor to help you make sure it is the right artist, sometimes not present)
- **Release feed** — browse albums, EPs and singles sorted by date, filter by artist, type, or new-only
- **Fetch tracklist** — Fetches tracklist for EP and Album release.
- **Singles detection** — When fetching tracklist, if single release of a song exists, it will be marked with "SINGLE" badge. Useful if you want to avoid duplicates.
- **New release detection** — "Check Now" scans all tracked artists and highlights newly discovered releases with a badge
- **Official + Unofficial releases** — This can be changed in the musicbrainz.py.
- **Shortcut to MB page** Click on "view" button to see individual release page on Musicbrainz. similarly, click on artist to see their page.
- **Easy backup** — Allows users to import & export list of artists in a .json file

## Quick Start

```bash
git clone https://github.com/Jawsled/music-release-tracker.git
cd music-release-tracker
python run.py
```

That's it. On first run it creates a virtual environment and installs dependencies automatically. Your browser opens to `http://127.0.0.1:7070`.

## Desktop Shortcut (optional)

- Includes conda launch shortcut, if for some strange reason you want to run it in anaconda when it is already running in venv. But .bat file is there if you want it.
- conda environment name should be music-release-tracker. 


## How It Works

1. **Add artists** in the Artists tab — search by name, pick from results
2. **Check for new releases** in the Feed tab, there is "check now" button.
3. **Browse your feed** in the Feed tab — new releases are highlighted with a "NEW" badge; click to dismiss

When you first add an artist, all their existing releases are imported as "already seen" so your feed isn't flooded. Only releases discovered in subsequent checks are marked as new.

## Telegram (removed)

- Telegram support has been dropped.

## Tech Stack

- **Backend:** Python, FastAPI, SQLite
- **Frontend:** Vanilla HTML/CSS/JS, dark theme
- **Data source:** [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API) (no API key required)

## Requirements

- Python 3.8 or newer
- Internet connection (for MusicBrainz API)

All Python dependencies are installed automatically by `run.py`.


## Planned future update
- **iTunes/apple music support** - As great as Musicbrainz is, it can take several days to be updated. This planned update will try to remedy this. 
