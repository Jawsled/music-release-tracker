#!/usr/bin/env python3
"""Interactive Telegram bot for managing the music release tracker.

Commands:
    /start, /help  — Show available commands
    /artists        — List tracked artists
    /add <name>     — Search and add an artist
    /remove         — Remove a tracked artist
    /check          — Check all artists for new releases
    /releases       — Show recent releases (last 20)
    /unseen         — Show only new/unseen releases

Usage:
    python telegram_bot.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx

import db
import musicbrainz

CONFIG_PATH = Path(__file__).parent / "telegram_config.json"
TELEGRAM_API = "https://api.telegram.org/bot{token}"

COVER_ART_URL = "https://coverartarchive.org/release-group/{mbid}/front-500"

NOTIFICATION_HOUR = 10  # Daily scheduled check fires at this hour (local time)
LAST_CHECK_KEY = "last_scheduled_check"

HELP_TEXT = (
    "\U0001f3b5 <b>Music Release Tracker Bot</b>\n"
    "\n"
    "Available commands:\n"
    "/artists — List tracked artists\n"
    "/add &lt;name&gt; — Search and add an artist\n"
    "/remove — Remove a tracked artist\n"
    "/check — Check all artists for new releases\n"
    "/releases — Show recent releases (last 20)\n"
    "/unseen — Show only new/unseen releases\n"
    "/cover — Show album cover art\n"
    "/today — Show releases that come out today\n"
    "/help — Show this message"
)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print("Error: telegram_config.json not found. Run 'python notify.py --setup' first.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


async def api_request(token: str, method: str, **kwargs) -> dict:
    """Make a Telegram Bot API request."""
    url = f"{TELEGRAM_API.format(token=token)}/{method}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=kwargs)
        return resp.json()


async def send_message(token: str, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
    """Send a text message, optionally with an inline keyboard."""
    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        params["reply_markup"] = reply_markup
    return await api_request(token, "sendMessage", **params)


async def send_photo(token: str, chat_id: str, photo_url: str, caption: str = "") -> dict:
    """Send a photo by URL."""
    params = {"chat_id": chat_id, "photo": photo_url}
    if caption:
        params["caption"] = caption
        params["parse_mode"] = "HTML"
    return await api_request(token, "sendPhoto", **params)


async def answer_callback_query(token: str, callback_query_id: str, text: str = "") -> dict:
    """Acknowledge a callback query to dismiss the loading spinner."""
    params = {"callback_query_id": callback_query_id}
    if text:
        params["text"] = text
    return await api_request(token, "answerCallbackQuery", **params)


async def get_updates(token: str, offset: int, timeout: int = 30) -> list[dict]:
    """Long-poll for updates from Telegram."""
    url = f"{TELEGRAM_API.format(token=token)}/getUpdates"
    async with httpx.AsyncClient(timeout=timeout + 10) as client:
        resp = await client.post(url, json={
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        })
        data = resp.json()
        return data.get("result", [])


# --- Command handlers ---

async def cmd_help(token: str, chat_id: str):
    await send_message(token, chat_id, HELP_TEXT)


async def cmd_artists(token: str, chat_id: str):
    artists = db.get_all_artists()
    if not artists:
        await send_message(token, chat_id, "No artists tracked yet. Use /add &lt;name&gt; to add one.")
        return

    lines = [f"\U0001f3a4 <b>Tracked artists ({len(artists)}):</b>\n"]
    for a in artists:
        disambig = f" ({a['disambiguation']})" if a.get("disambiguation") else ""
        lines.append(f"  \u2022 {a['name']}{disambig}")
    await send_message(token, chat_id, "\n".join(lines))


async def cmd_add(token: str, chat_id: str, query: str):
    if not query:
        await send_message(token, chat_id, "Usage: /add &lt;artist name&gt;\nExample: /add Radiohead")
        return

    await send_message(token, chat_id, f"\U0001f50d Searching for \"{query}\"...")

    try:
        results = await musicbrainz.search_artist(query)
    except Exception as e:
        await send_message(token, chat_id, f"Search failed: {e}")
        return

    if not results:
        await send_message(token, chat_id, "No results found.")
        return

    # Show top 5 results as inline buttons
    top = results[:5]
    buttons = []
    for r in top:
        label = r["name"]
        if r.get("disambiguation"):
            label += f" ({r['disambiguation']})"
        if r.get("country"):
            label += f" [{r['country']}]"
        buttons.append([{"text": label, "callback_data": f"add:{r['mbid']}"}])

    keyboard = {"inline_keyboard": buttons}
    await send_message(token, chat_id, "Select an artist to add:", reply_markup=keyboard)


async def cmd_remove(token: str, chat_id: str):
    artists = db.get_all_artists()
    if not artists:
        await send_message(token, chat_id, "No artists to remove.")
        return

    buttons = []
    for a in artists:
        label = a["name"]
        if a.get("disambiguation"):
            label += f" ({a['disambiguation']})"
        buttons.append([{"text": f"\u274c {label}", "callback_data": f"rm:{a['id']}"}])

    keyboard = {"inline_keyboard": buttons}
    await send_message(token, chat_id, "Select an artist to remove:", reply_markup=keyboard)


async def cmd_check(token: str, chat_id: str):
    from notify import run_check, format_message

    artists = db.get_all_artists()
    if not artists:
        await send_message(token, chat_id, "No artists tracked. Add some first with /add.")
        return

    await send_message(token, chat_id, f"\U0001f504 Checking {len(artists)} artist(s) for new releases...")

    summary = await run_check()

    if not summary:
        await send_message(token, chat_id, "\u2705 No new releases found.")
    else:
        msg = format_message(summary)
        await send_message(token, chat_id, msg)


async def cmd_releases(token: str, chat_id: str):
    releases = db.get_releases()
    if not releases:
        await send_message(token, chat_id, "No releases in the database yet.")
        return

    recent = releases[:20]
    lines = [f"\U0001f4bf <b>Recent releases ({len(recent)} of {len(releases)}):</b>\n"]
    for r in recent:
        date_part = f" \u2014 {r['release_date']}" if r.get("release_date") else ""
        new_badge = " \U0001f195" if r.get("notified") == 0 else ""
        lines.append(f"  \u2022 <b>{r['artist_name']}</b> \u2014 {r['title']} ({r['release_type']}){date_part}{new_badge}")
    await send_message(token, chat_id, "\n".join(lines))


async def cmd_unseen(token: str, chat_id: str):
    releases = db.get_releases(unseen_only=True)
    if not releases:
        await send_message(token, chat_id, "\u2705 No unseen releases. You're all caught up!")
        return

    lines = [f"\U0001f195 <b>Unseen releases ({len(releases)}):</b>\n"]
    for r in releases:
        date_part = f" \u2014 {r['release_date']}" if r.get("release_date") else ""
        lines.append(f"  \u2022 <b>{r['artist_name']}</b> \u2014 {r['title']} ({r['release_type']}){date_part}")
    await send_message(token, chat_id, "\n".join(lines))


async def cmd_cover(token: str, chat_id: str):
    releases = db.get_releases()
    if not releases:
        await send_message(token, chat_id, "No releases in the database yet.")
        return

    recent = releases[:10]
    buttons = []
    for r in recent:
        label = f"{r['artist_name']} — {r['title']}"
        if len(label) > 60:
            label = label[:57] + "..."
        buttons.append([{"text": label, "callback_data": f"cover:{r['mbid']}"}])

    keyboard = {"inline_keyboard": buttons}
    await send_message(token, chat_id, "\U0001f3a8 Select a release to see its cover:", reply_markup=keyboard)


async def cmd_today(token: str, chat_id: str):
    from notify import check_release_day, format_release_day_message

    releases = check_release_day()
    if not releases:
        await send_message(token, chat_id, "\u2705 No releases coming out today.")
    else:
        msg = format_release_day_message(releases)
        await send_message(token, chat_id, msg)


# --- Callback handlers ---

async def handle_add_callback(token: str, chat_id: str, mbid: str):
    """Handle artist selection from /add search results."""
    # Check if already tracked
    existing = db.get_artist_by_mbid(mbid)
    if existing:
        await send_message(token, chat_id, f"\u2139\ufe0f <b>{existing['name']}</b> is already tracked.")
        return

    # Fetch artist details from search to get name/disambiguation
    try:
        results = await musicbrainz.search_artist(mbid)
    except Exception:
        results = []

    # Try to find the exact artist by MBID in results, or fetch via search
    artist_info = None
    for r in results:
        if r["mbid"] == mbid:
            artist_info = r
            break

    if not artist_info:
        # Fallback: use the MusicBrainz lookup API directly
        try:
            data = await musicbrainz._rate_limited_get(
                f"{musicbrainz.BASE_URL}/artist/{mbid}",
                params={"fmt": "json"},
            )
            artist_info = {
                "mbid": data["id"],
                "name": data.get("name", "Unknown"),
                "disambiguation": data.get("disambiguation", ""),
            }
        except Exception as e:
            await send_message(token, chat_id, f"Failed to fetch artist info: {e}")
            return

    # Add artist to database
    artist = db.add_artist(
        mbid=artist_info["mbid"],
        name=artist_info["name"],
        disambiguation=artist_info.get("disambiguation", ""),
    )

    await send_message(token, chat_id, f"\u2705 Added <b>{artist['name']}</b>! Importing existing releases...")

    # Import existing releases as already seen
    try:
        releases = await musicbrainz.get_artist_releases(mbid)
        count = 0
        for rel in releases:
            inserted = db.add_release(
                mbid=rel["mbid"],
                artist_id=artist["id"],
                title=rel["title"],
                release_type=rel["type"],
                release_date=rel["date"],
                notified=1,  # Mark as already seen
            )
            if inserted:
                count += 1
        await send_message(token, chat_id, f"Imported {count} existing release(s). Future checks will detect new ones.")
    except Exception as e:
        await send_message(token, chat_id, f"Artist added, but failed to import releases: {e}")


async def handle_remove_callback(token: str, chat_id: str, artist_id_str: str):
    """Handle artist selection from /remove list."""
    try:
        artist_id = int(artist_id_str)
    except ValueError:
        await send_message(token, chat_id, "Invalid artist ID.")
        return

    # Get artist name before removing
    artists = db.get_all_artists()
    artist_name = "Unknown"
    for a in artists:
        if a["id"] == artist_id:
            artist_name = a["name"]
            break

    db.remove_artist(artist_id)
    await send_message(token, chat_id, f"\U0001f5d1 Removed <b>{artist_name}</b> and all their releases.")


async def handle_cover_callback(token: str, chat_id: str, release_mbid: str):
    """Send album cover art for a release."""
    # Find release info for the caption
    releases = db.get_releases()
    caption = ""
    for r in releases:
        if r["mbid"] == release_mbid:
            caption = f"<b>{r['artist_name']}</b> — {r['title']}"
            break

    cover_url = COVER_ART_URL.format(mbid=release_mbid)

    # Check if cover exists before sending
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        try:
            head_resp = await client.head(cover_url)
            if head_resp.status_code != 200:
                await send_message(token, chat_id, "No cover art available for this release.")
                return
        except Exception:
            await send_message(token, chat_id, "Failed to fetch cover art.")
            return

    result = await send_photo(token, chat_id, cover_url, caption)
    if not result.get("ok"):
        await send_message(token, chat_id, "No cover art available for this release.")


# --- Scheduled daily check ---

def _seconds_until_next_run() -> float:
    now = datetime.now()
    target = now.replace(hour=NOTIFICATION_HOUR, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _run_scheduled_check(token: str, chat_id: str):
    from notify import run_check, format_message, check_release_day, format_release_day_message

    summary = await run_check()
    if summary:
        await send_message(token, chat_id, format_message(summary))

    due_today = check_release_day()
    if due_today:
        result = await send_message(token, chat_id, format_release_day_message(due_today))
        if result.get("ok"):
            for r in due_today:
                db.mark_release_day_notified(r["id"])

    db.set_meta(LAST_CHECK_KEY, datetime.now().strftime("%Y-%m-%d"))


async def scheduled_check_loop(token: str, chat_id: str):
    """Run the daily check at NOTIFICATION_HOUR local time.

    On startup, if today's run was missed (bot was down at fire time), catch up.
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.hour >= NOTIFICATION_HOUR and db.get_meta(LAST_CHECK_KEY) != today:
        print(f"Scheduler: catching up on missed run for {today}")
        try:
            await _run_scheduled_check(token, chat_id)
        except Exception as e:
            print(f"Scheduler catch-up failed: {e}")

    while True:
        delay = _seconds_until_next_run()
        next_at = datetime.now() + timedelta(seconds=delay)
        print(f"Scheduler: next run at {next_at.strftime('%Y-%m-%d %H:%M:%S')} (in {int(delay)}s)")
        await asyncio.sleep(delay)
        try:
            await _run_scheduled_check(token, chat_id)
        except Exception as e:
            print(f"Scheduled check failed: {e}")


# --- Main loop ---

async def handle_message(token: str, chat_id: str, message: dict):
    """Route incoming messages to command handlers."""
    text = message.get("text", "").strip()
    if not text:
        return

    # Parse command and arguments
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        command = parts[0].lower().split("@")[0]  # Strip @botname suffix
        args = parts[1] if len(parts) > 1 else ""
    else:
        return  # Ignore non-command messages

    if command in ("/start", "/help"):
        await cmd_help(token, chat_id)
    elif command == "/artists":
        await cmd_artists(token, chat_id)
    elif command == "/add":
        await cmd_add(token, chat_id, args)
    elif command == "/remove":
        await cmd_remove(token, chat_id)
    elif command == "/check":
        await cmd_check(token, chat_id)
    elif command == "/releases":
        await cmd_releases(token, chat_id)
    elif command == "/unseen":
        await cmd_unseen(token, chat_id)
    elif command == "/cover":
        await cmd_cover(token, chat_id)
    elif command == "/today":
        await cmd_today(token, chat_id)
    else:
        await send_message(token, chat_id, "Unknown command. Use /help to see available commands.")


async def handle_callback(token: str, chat_id: str, callback_query: dict):
    """Route callback queries from inline keyboard buttons."""
    data = callback_query.get("data", "")
    callback_id = callback_query["id"]

    if data.startswith("add:"):
        mbid = data[4:]
        await answer_callback_query(token, callback_id, "Adding artist...")
        await handle_add_callback(token, chat_id, mbid)
    elif data.startswith("rm:"):
        artist_id_str = data[3:]
        await answer_callback_query(token, callback_id, "Removing artist...")
        await handle_remove_callback(token, chat_id, artist_id_str)
    elif data.startswith("cover:"):
        release_mbid = data[6:]
        await answer_callback_query(token, callback_id, "Fetching cover...")
        await handle_cover_callback(token, chat_id, release_mbid)
    else:
        await answer_callback_query(token, callback_id, "Unknown action.")


async def main():
    config = load_config()
    token = config["bot_token"]
    chat_id = config["chat_id"]

    db.init_db()

    asyncio.create_task(scheduled_check_loop(token, chat_id))

    print("Bot started. Listening for messages... (Ctrl+C to stop)")

    offset = 0
    while True:
        try:
            updates = await get_updates(token, offset)
        except httpx.TimeoutException:
            continue
        except Exception as e:
            print(f"Error fetching updates: {e}")
            await asyncio.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1

            if "message" in update:
                msg = update["message"]
                msg_chat_id = str(msg["chat"]["id"])
                if msg_chat_id != chat_id:
                    await send_message(token, msg_chat_id, "\U0001f6ab Sorry, this bot is private.")
                    continue
                await handle_message(token, chat_id, msg)

            elif "callback_query" in update:
                cb = update["callback_query"]
                cb_chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                if cb_chat_id != chat_id:
                    await answer_callback_query(token, cb["id"], "Not authorized.")
                    continue
                await handle_callback(token, chat_id, cb)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped.")
