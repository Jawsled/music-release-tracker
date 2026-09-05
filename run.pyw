"""
Music Release Tracker — Click-to-run launcher (no terminal window).

On Windows: double-click this file to start the app.
On Linux/Mac: run with 'pythonw run.pyw' or use the desktop shortcut.

On first run, a small setup window will appear while dependencies install.
On subsequent runs, the browser opens directly.
"""

import os
import subprocess
import sys
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
# Keep the legacy launcher environment separate from uv's project environment.
VENV_DIR = APP_DIR / ".venv-run"
REQUIREMENTS = APP_DIR / "requirements.txt"
MARKER = VENV_DIR / ".installed"

if sys.platform == "win32":
    PYTHON = VENV_DIR / "Scripts" / "python.exe"
    PIP = VENV_DIR / "Scripts" / "pip.exe"
else:
    PYTHON = VENV_DIR / "bin" / "python"
    PIP = VENV_DIR / "bin" / "pip"

# Hide subprocess console windows on Windows
CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def needs_setup():
    if not VENV_DIR.exists():
        return True
    if not MARKER.exists():
        return True
    current = REQUIREMENTS.read_text().strip()
    return MARKER.read_text().strip() != current


def run_setup():
    if not VENV_DIR.exists():
        subprocess.check_call(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            creationflags=CREATION_FLAGS,
        )
    current = REQUIREMENTS.read_text().strip()
    if not MARKER.exists() or MARKER.read_text().strip() != current:
        subprocess.check_call(
            [str(PIP), "install", "-q", "-r", str(REQUIREMENTS)],
            creationflags=CREATION_FLAGS,
        )
        MARKER.write_text(current)


def run_app():
    subprocess.Popen(
        [str(PYTHON), str(APP_DIR / "app.py")],
        cwd=str(APP_DIR),
        creationflags=CREATION_FLAGS,
    )


def main():
    os.chdir(str(APP_DIR))

    if needs_setup():
        # Show a simple Tk progress window during first-time setup
        try:
            import tkinter as tk

            root = tk.Tk()
            root.title("Music Release Tracker")
            root.geometry("350x80")
            root.resizable(False, False)
            label = tk.Label(root, text="Setting up for first run...\nThis only happens once.")
            label.pack(expand=True)

            def setup_then_launch():
                run_setup()
                root.after(0, root.destroy)

            threading.Thread(target=setup_then_launch, daemon=True).start()
            root.mainloop()
        except ImportError:
            # No tkinter available, just run setup silently
            run_setup()

    run_app()


if __name__ == "__main__":
    main()
