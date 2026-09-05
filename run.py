"""
Music Release Tracker — Launcher

Run this script to start the app. On first run it will:
  1. Create a launcher-only virtual environment (.venv-run/)
  2. Install dependencies from requirements.txt
  3. Start the app and open your browser

On subsequent runs it skips straight to step 3.

Usage:
    python run.py
"""


import os
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
# Keep the legacy launcher environment separate from uv's project environment.
VENV_DIR = APP_DIR / ".venv-run"
REQUIREMENTS = APP_DIR / "requirements.txt"
MARKER = VENV_DIR / ".installed"

# Detect platform-appropriate paths
if sys.platform == "win32":
    PYTHON = VENV_DIR / "Scripts" / "python.exe"
    PIP = VENV_DIR / "Scripts" / "pip.exe"
else:
    PYTHON = VENV_DIR / "bin" / "python"
    PIP = VENV_DIR / "bin" / "pip"


def create_venv():
    if VENV_DIR.exists():
        return
    print("[*] Creating virtual environment...")
    subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
    print("[+] Virtual environment created.\n")


def install_requirements():
    """Install/update requirements if they've changed since last install."""
    current_hash = REQUIREMENTS.read_text().strip()
    if MARKER.exists() and MARKER.read_text().strip() == current_hash:
        return
    print("[*] Installing dependencies...")
    subprocess.check_call(
        [str(PIP), "install", "-q", "-r", str(REQUIREMENTS)],
    )
    MARKER.write_text(current_hash)
    print("[+] Dependencies installed.\n")


def run_app():
    print("[*] Starting Music Release Tracker...\n")
    try:
        subprocess.check_call(
            [str(PYTHON), str(APP_DIR / "app.py")],
            cwd=str(APP_DIR),
        )
    except KeyboardInterrupt:
        print("\n[*] Stopped.")


def main():
    os.chdir(str(APP_DIR))

    if not REQUIREMENTS.exists():
        print("[!] requirements.txt not found.")
        sys.exit(1)

    create_venv()
    install_requirements()
    run_app()


if __name__ == "__main__":
    main()
