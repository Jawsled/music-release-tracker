"""Central in-memory log store shared by the UI Logs panel and Python logging.

Anything logged on the "music-release-tracker" logger tree (e.g. retries,
timeouts, rate limits inside musicbrainz.py / itunes.py) is captured by
UILogHandler and becomes visible in the UI via GET /api/logs.
"""
from __future__ import annotations

import itertools
import logging
import threading
from collections import deque
from datetime import datetime

MAX_LOG_ENTRIES = 500

_buffer: deque[dict] = deque(maxlen=MAX_LOG_ENTRIES)
_counter = itertools.count()
_lock = threading.Lock()

# Level names as expected by the UI CSS (.log-level.INFO/.WARN/.ERROR)
_LEVEL_MAP = {"WARNING": "WARN", "CRITICAL": "ERROR", "FATAL": "ERROR"}


def add_log(level: str, message: str, artist: str = "", detail: str = "") -> dict:
    """Append an entry to the UI log buffer (and mirror it to console logging)."""
    entry = {
        "id": next(_counter),
        "timestamp": datetime.now().isoformat(),
        "level": _LEVEL_MAP.get(level.upper(), level.upper()),
        "message": message,
        "artist": artist,
        "detail": detail,
    }
    with _lock:
        _buffer.append(entry)

    # Mirror to console. The record is marked so UILogHandler skips it
    # (otherwise every entry would be duplicated in the UI buffer).
    lg = logging.getLogger("music-release-tracker")
    console_fn = {
        "INFO": lg.info,
        "WARN": lg.warning,
        "ERROR": lg.error,
    }.get(entry["level"], lg.info)
    try:
        console_fn(
            f"[{artist or 'SCAN'}] {message}" + (f" - {detail}" if detail else ""),
            extra={"mrt_ui_logged": True},
        )
    except Exception:
        pass
    return entry


class UILogHandler(logging.Handler):
    """Forwards standard logging records into the UI log buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if getattr(record, "mrt_ui_logged", False):
                return  # already stored via add_log()
            artist = getattr(record, "artist", "") or ""
            detail = getattr(record, "detail", "") or ""
            add_log(
                record.levelname,
                record.getMessage(),
                artist=artist,
                detail=detail,
            )
        except Exception:
            self.handleError(record)


def attach_handler(logger_name: str = "music-release-tracker") -> None:
    """Install the UI handler once on the given logger (idempotent)."""
    lg = logging.getLogger(logger_name)
    if not any(isinstance(h, UILogHandler) for h in lg.handlers):
        handler = UILogHandler()
        handler.setLevel(logging.INFO)
        lg.addHandler(handler)


def get_logs(limit: int = 100) -> list[dict]:
    entries = list(_buffer)
    return entries[-max(0, limit):]


def clear_logs() -> None:
    with _lock:
        _buffer.clear()
