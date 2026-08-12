# Music Release Tracker

<<<<<<< HEAD
A local-first desktop app that tracks new album and EP releases from artists you follow, powered by [MusicBrainz](https://musicbrainz.org/).
=======
A local-first desktop app that tracks new album and EP releases from artists you follow, powered by [MusicBrainz](https://musicbrainz.org/) and [iTunes](https://music.apple.com)
>>>>>>> 2b67ae5 (updated readme and requirements)

![Dark themed UI with three tabs: Feed, Artists, and Check](https://img.shields.io/badge/theme-dark-1a1a2e) ![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-bb86fc) ![License: MIT](https://img.shields.io/badge/license-MIT-03dac6)

## Features

<<<<<<< HEAD
- **Artist search** — find and add artists from MusicBrainz with disambiguation (Short descriptor to help you make sure it is the right artist, sometimes not present)
- **Release feed** — browse albums, EPs and singles sorted by date, filter by artist, type, or new-only
- **Fetch tracklist** — Fetches tracklist for EP and Album release.
- **Singles detection** — When fetching tracklist, if single release of a song exists, it will be marked with "SINGLE" badge. Useful if you want to avoid duplicates.
- **New release detection** — "Check Now" scans all tracked artists and highlights newly discovered releases with a badge
- **Official + Unofficial releases** — This can be changed in the musicbrainz.py file. This is done to catch pre-official release such as ones on soundcloud, assuming that gets caught and put on MB before official release.
- **Shortcut to MB page** Click on "view" button to see individual release page on Musicbrainz. similarly, click on artist to see their page.
- **Easy backup** — Allows users to import & export list of artists in a .json file
=======
- **Artist search:** search artists from MusicBrainz and iTunes, or paste mbid, musicbrainz artist URL, or apple Music artist URL manually. You are also able to click on MB / iTunes button to confirm that you are adding the right artist.
- **Release feed:** browse albums, EPs and singles sorted by date, filter by artist, type, or new-only
- **Fetch tracklist:** Fetches tracklist for EP and Album releases. Includes number of tracks and lenghts for each tracks.
- **Singles detection:** When fetching tracklist, if single release of a song exists, it will be marked with "SINGLE" badge. Useful if you want to avoid duplicates.
- **Duplicates detection:** If there is Duplicate release between Musicbrainz and iTunes, MB result will be prioritized. iTunes results are treated as complementary to catch releases that are not (yet) on MB.
- **New release detection:** "Check Now" scans all tracked artists and highlights newly discovered releases with a badge
- **Official releases:** It will fetch basically everything from "releases" tab from artists' MB page. this is done to include not only regular releases but pre-release, remixes, and Mixes / compilation such as DJ sets.
- **Shortcut to release page:** Click on "view" button to see individual release page on Musicbrainz or apple Music. similarly, click on artist to see their page.
- **Easy backup** — Allows users to import & export list of artists in a .json file which includes their name, mbid and iTunes artist ID. or alternatively you can make a copy of .db file in /data directory which also includes scanned releases.
>>>>>>> 2b67ae5 (updated readme and requirements)

## Quick Start

```bash
git clone https://github.com/Jawsled/music-release-tracker.git
cd music-release-tracker
python run.py
```

<<<<<<< HEAD
That's it. On first run it creates a virtual environment and installs dependencies automatically. Your browser opens to `http://127.0.0.1:7070`.
=======
Simple as that. On first run it creates a virtual environment and installs dependencies automatically. You wiill be directed to the webUI `http://127.0.0.1:7070`.
>>>>>>> 2b67ae5 (updated readme and requirements)

## Desktop Shortcut (optional)

- Includes conda launch shortcut, if for some strange reason you want to run it in anaconda when it is already running in venv. But .bat file is there if you want it.
- conda environment name should be music-release-tracker. 


<<<<<<< HEAD
## How It Works

1. **Add artists** in the Artists tab — search by name, pick from results
2. **Check for new releases** in the Feed tab, there is "check now" button.
3. **Browse your feed** in the Feed tab — new releases are highlighted with a "NEW" badge; click to dismiss

When you first add an artist, all their existing releases are imported as "already seen" so your feed isn't flooded. Only releases discovered in subsequent checks are marked as new.

## Telegram (removed)

- Telegram support has been dropped.
=======
## How to use

1. **Add artists:** search by name and pick from results, or paste in URL if the search did not pick up the artist you want.
2. **Check for new releases:** The check button acknowledges which artists are being selected from the dropdown menu in the feed. If all artists are shown, it will update all artist entries, if one specific artist is selected, it will only check said artist to make the process quick and avoid being rate limited.
3. **Browse your feed:** Each releases contain Title, releast type, date, and source (MB/iTunes) as well ast cover image. newly added releases will have "NEW" badge, click to dismiss individually, or press "Mark all as seen" button. If searching for particular release, use browser's search function (Ctrl+F).

When you first add an artist, all their existing releases are imported as "already seen" so your feed isn't flooded. Only releases discovered in subsequent checks are marked as new.

## Known issues
- **Quirks with iTunes API:** Due to iTunes not providing release type and labels everything as "collectiontype=album",  I had to resort to using suffix "- Single" and "- EP" to classify them as such. Lacking those, it will be classified as an album. This is unfortunately not fixable without using the modern apple music API key. This also means we are working with less frequently maintained dataset, which may not reflect full release catalogue (sometimes you need to use UPC lookup to find missing releases).
- The program is fairly good at finding duplicates and hiding iTunes release if it deems such, but if the title is slightly different, it can still show up. I plan to fix this down the line, however it is not as simple as treating all (feat. fetured-artist) becausee some tracks get additional featured artists, which then gets a new release, and there also no way to tell this apart.

## Telegram (removed)

- Telegram support has been dropped since a significant portion of code has been re-written and I did not deem this feature necessary.
- However, if you wish to add your own integrations to Telegram / Discord and such, please feel free to do so.
>>>>>>> 2b67ae5 (updated readme and requirements)

## Tech Stack

- **Backend:** Python, FastAPI, SQLite
- **Frontend:** Vanilla HTML/CSS/JS, dark theme
<<<<<<< HEAD
- **Data source:** [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API) (no API key required)
=======
- **Data source:** [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API) and [iTunes search API](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI) (no API keys required) 
>>>>>>> 2b67ae5 (updated readme and requirements)

## Requirements

- Python 3.8 or newer
<<<<<<< HEAD
- Internet connection (for MusicBrainz API)

All Python dependencies are installed automatically by `run.py`.


## Planned future update
- **iTunes/apple music support** - As great as Musicbrainz is, it can take several days to be updated. This planned update will try to remedy this. 
=======
- Internet connection (for MusicBrainz / iTunes API)

All Python dependencies are installed automatically by `run.py`. If not, reference requirements.txt.

## Disclosure on LLM use
- The original release by Katenimps was co-authored with Claude.
- This fork contains bugfixes and feature additions assisted by Qwen3.6.
>>>>>>> 2b67ae5 (updated readme and requirements)
