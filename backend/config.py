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
    Optimized low-RAM formula:
    Each Uvicorn worker costs ~55-70 MB RAM. 
    Using 2 workers is ideal for low RAM environments while maintaining high throughput.
    """
    return int(os.getenv("WORKERS", "2"))

WORKERS: int = int(os.getenv("WORKERS", str(_default_workers())))

BASE_DIR = Path(__file__).parent

DATA_DIR: Path = BASE_DIR / os.getenv("DATA_DIR", "data")

QUESTIONS_FILE: Path | None = (
    BASE_DIR / os.getenv("QUESTIONS_FILE") if os.getenv("QUESTIONS_FILE") else None
)
ANSWERS_FILE: Path | None = (
    BASE_DIR / os.getenv("ANSWERS_FILE") if os.getenv("ANSWERS_FILE") else None
)

ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "changeme")
