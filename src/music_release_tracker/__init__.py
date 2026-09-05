"""uv entry point for the repository-local application."""

import os
import runpy
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2]


def main() -> None:
    """Start the application using uv's project environment."""
    os.chdir(APP_DIR)
    sys.path.insert(0, str(APP_DIR))
    runpy.run_path(str(APP_DIR / "app.py"), run_name="__main__")
