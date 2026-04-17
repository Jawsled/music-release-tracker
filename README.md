# Music Release Tracker

A local-first desktop app that tracks new album and EP releases from artists you follow, powered by [MusicBrainz](https://musicbrainz.org/).

![Dark themed UI with three tabs: Feed, Artists, and Check](https://img.shields.io/badge/theme-dark-1a1a2e) ![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-bb86fc) ![License: MIT](https://img.shields.io/badge/license-MIT-03dac6)

## Features

- **Artist search** — find and add artists from MusicBrainz with disambiguation support
- **Release feed** — browse albums and EPs sorted by date, filter by artist, type, or new-only
- **New release detection** — "Check Now" scans all tracked artists and highlights newly discovered releases with a badge
- **Official releases only** — automatically filters out bootlegs, compilations, live albums, soundtracks, and remixes
- **Telegram notifications** — daily bot pings for newly discovered releases and release-day drops
- **Click-to-run** — auto-installs dependencies on first launch, no manual setup needed

## Quick Start

```bash
git clone https://github.com/katemptiness/music-release-tracker.git
cd music-release-tracker
python run.py
```

That's it. On first run it creates a virtual environment and installs dependencies automatically. Your browser opens to `http://127.0.0.1:8080`.

## Desktop Shortcut (optional)

To add the app to your application menu (Linux) or Desktop (Windows):

```bash
python create-shortcut.py
```

## How It Works

1. **Add artists** in the Artists tab — search by name, pick from results
2. **Check for new releases** in the Check tab — the app fetches release data from MusicBrainz with live progress
3. **Browse your feed** in the Feed tab — new releases are highlighted with a "NEW" badge; click to dismiss

When you first add an artist, all their existing releases are imported as "already seen" so your feed isn't flooded. Only releases discovered in subsequent checks are marked as new.

## Telegram Setup (optional)

Connect a Telegram bot to get notifications and manage your library from your phone:

```bash
python notify.py --setup
```

This walks you through creating a bot via @BotFather and saves your token and chat ID to `telegram_config.json`. Once configured, start the interactive bot (next section) to get automatic daily notifications.

You can also run `python notify.py` manually at any time for a one-shot check plus notification.

## Interactive Telegram Bot (optional)

Manage your entire music library from Telegram — and get automatic daily notifications — no need to open the web UI:

```bash
python telegram_bot.py
```

This starts a long-polling bot that:

- Runs a daily check at **10:00 local time**, pinging you about newly discovered releases and any tracked albums that drop that day.
- Catches up on startup if the bot was down when the daily run was scheduled.
- Responds to these commands:

| Command | Description |
|---------|-------------|
| `/start`, `/help` | Show available commands |
| `/artists` | List tracked artists |
| `/add <name>` | Search MusicBrainz, pick from inline buttons to add |
| `/remove` | Pick a tracked artist to remove via inline buttons |
| `/check` | Check all artists for new releases |
| `/releases` | Show recent releases (last 20) |
| `/unseen` | Show only new/unseen releases |
| `/cover` | Browse and view album cover art |
| `/today` | Show releases that come out today |

Uses the same `telegram_config.json` from setup. Run it as a systemd user service (recommended) or in tmux/screen to keep it running 24/7 — the scheduled daily notifications only fire while the bot is running, with catch-up on next startup.

## Tech Stack

- **Backend:** Python, FastAPI, SQLite
- **Frontend:** Vanilla HTML/CSS/JS, dark theme
- **Data source:** [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API) (no API key required)

## Requirements

- Python 3.8 or newer
- Internet connection (for MusicBrainz API)

All Python dependencies are installed automatically by `run.py`.
