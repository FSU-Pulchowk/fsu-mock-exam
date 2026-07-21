"""
config.py — centralised settings loaded from environment / .env file.

All values can be overridden by creating a .env file (copy from .env.example).
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the backend directory
load_dotenv(Path(__file__).parent / ".env")


def _parse_origins(raw: str) -> list[str]:
    """Split comma-separated CORS origins."""
    return [o.strip() for o in raw.split(",") if o.strip()]


# ── Exam window ───────────────────────────────────────────────────────────────
EXAM_START: str | None = os.getenv("EXAM_START")  # ISO 8601 string or None
EXAM_END: str | None = os.getenv("EXAM_END")       # ISO 8601 string or None

# ── CORS ─────────────────────────────────────────────────────────────────────
CORS_ORIGINS: list[str] = _parse_origins(os.getenv("CORS_ORIGINS", "*"))

# ── Rate limiting ─────────────────────────────────────────────────────────────
RATE_LIMIT: str = os.getenv("RATE_LIMIT", "120/minute")

# ── Server ────────────────────────────────────────────────────────────────────
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "6090"))
ROOT_PATH: str = os.getenv("ROOT_PATH", "")

# ── Worker count ─────────────────────────────────────────────────────────────
import multiprocessing as _mp

def _default_workers() -> int:
    """
    Conservative formula that fits in 2.3 GB RAM:
    each Uvicorn worker costs ~55-70 MB; reserve 400 MB for OS + cache.
    Never exceed 2 × CPU + 1 (diminishing returns on I/O-bound async work).
    """
    cpu_based = min(_mp.cpu_count() * 2 + 1, 8)
    ram_mb = 2300
    overhead_mb = 400
    per_worker_mb = 65
    ram_based = (ram_mb - overhead_mb) // per_worker_mb
    return max(1, min(cpu_based, ram_based))

WORKERS: int = int(os.getenv("WORKERS", str(_default_workers())))

# ── Data files ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
QUESTIONS_FILE: Path = BASE_DIR / os.getenv("QUESTIONS_FILE", "sets_new.json")
ANSWERS_FILE: Path = BASE_DIR / os.getenv("ANSWERS_FILE", "answers.json")
