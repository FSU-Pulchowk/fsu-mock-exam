"""
run.py — Production server launcher.

Usage:
    python run.py            # auto-configures workers based on CPU + RAM
    python run.py --dev      # hot-reload dev mode (single worker)

This file is a convenience wrapper.  For bare Uvicorn (dev):
    uvicorn main:app --reload --port 5000

For production Gunicorn:
    gunicorn main:app -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:5000

Windows note
────────────
Gunicorn does not support Windows (it uses Unix fork()).
On Windows, use Uvicorn directly (single-process, still async):
    uvicorn main:app --host 0.0.0.0 --port 5000 --workers 1

On Linux/Mac (production), Gunicorn + Uvicorn workers is preferred.
"""

import sys
import os
import platform
import subprocess
from pathlib import Path

# Change to the backend directory so relative file paths resolve correctly
os.chdir(Path(__file__).parent)

from config import HOST, PORT, WORKERS  # noqa: E402


def run_dev():
    """Hot-reload dev server — single process, auto-reload on file changes."""
    print(f"🔧 Dev mode — Uvicorn on http://{HOST}:{PORT} (hot-reload)")
    os.execvp(
        "uvicorn",
        ["uvicorn", "main:app",
         "--host", HOST,
         "--port", str(PORT),
         "--reload",
         "--log-level", "info"],
    )


def run_prod_unix():
    """Gunicorn + Uvicorn workers — Linux/macOS production."""
    print(f"🚀 Production mode — Gunicorn × {WORKERS} workers on http://{HOST}:{PORT}")
    os.execvp(
        "gunicorn",
        [
            "gunicorn", "main:app",
            "-k", "uvicorn.workers.UvicornWorker",
            "-w", str(WORKERS),
            "-b", f"{HOST}:{PORT}",
            "--timeout", "30",
            "--graceful-timeout", "10",
            "--keep-alive", "5",
            "--log-level", "info",
            "--access-logfile", "-",
            "--error-logfile", "-",
        ],
    )


def run_prod_windows():
    """
    Uvicorn on Windows — async I/O, no forking.
    For true multi-core on Windows, run multiple uvicorn processes behind Nginx.
    """
    print(f"🚀 Production mode (Windows) — Uvicorn on http://{HOST}:{PORT}")
    print("   ℹ️  Gunicorn is not supported on Windows. Using single Uvicorn process.")
    print("   ℹ️  For multi-core production on Windows, run multiple instances behind Nginx.")
    os.execvp(
        "uvicorn",
        ["uvicorn", "main:app",
         "--host", HOST,
         "--port", str(PORT),
         "--log-level", "info",
         "--loop", "asyncio"],  # uvloop not available on Windows
    )


if __name__ == "__main__":
    is_dev = "--dev" in sys.argv
    is_windows = platform.system() == "Windows"

    if is_dev:
        run_dev()
    elif is_windows:
        run_prod_windows()
    else:
        run_prod_unix()
