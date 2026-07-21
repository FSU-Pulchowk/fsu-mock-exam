"""
cache.py — In-memory data store loaded once at application startup.

Reading 46 KB of JSON from disk on every request is the single biggest
bottleneck in the original Flask app.  Instead we load everything once
at startup and serve directly from Python dicts for the entire lifetime
of the process.  Memory cost: ~500 KB per worker — negligible.
"""

import json
import logging
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

# ── Module-level singletons ───────────────────────────────────────────────────

_questions_data: dict[str, Any] | None = None
_answers_data: dict[str, Any] | None = None

# Submission store: { studentId: { "answers": {...}, "score": float } }
# In-memory only.  For persistence across restarts, flush to disk (see flush_submissions).
_submissions: dict[str, dict] = {}
_submissions_lock = Lock()


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_questions(path: Path) -> None:
    """Parse questions JSON and cache in memory.  Called once on startup."""
    global _questions_data
    try:
        with open(path, "r", encoding="utf-8") as f:
            _questions_data = json.load(f)
        logger.info("[CACHE] Questions loaded -- %d questions from '%s'",
                    len(_questions_data.get("questions", [])), path.name)
    except FileNotFoundError:
        logger.error("[ERROR] Questions file not found: %s", path)
        _questions_data = None
    except json.JSONDecodeError as exc:
        logger.error("[ERROR] Invalid JSON in questions file: %s", exc)
        _questions_data = None


def load_answers(path: Path) -> None:
    """Parse answers JSON and cache in memory.  Called once on startup."""
    global _answers_data
    try:
        with open(path, "r", encoding="utf-8") as f:
            _answers_data = json.load(f)
        logger.info("[CACHE] Answers loaded -- %d answers from '%s'",
                    len(_answers_data.get("answers", [])), path.name)
    except FileNotFoundError:
        logger.error("[ERROR] Answers file not found: %s", path)
        _answers_data = None
    except json.JSONDecodeError as exc:
        logger.error("[ERROR] Invalid JSON in answers file: %s", exc)
        _answers_data = None


# ── Accessors ─────────────────────────────────────────────────────────────────

def get_questions() -> dict[str, Any] | None:
    return _questions_data


def get_answers() -> dict[str, Any] | None:
    return _answers_data


def is_questions_loaded() -> bool:
    return _questions_data is not None


def is_answers_loaded() -> bool:
    return _answers_data is not None


# ── Submission store ──────────────────────────────────────────────────────────

def store_submission(student_id: str, answers: dict, score: float) -> None:
    """Thread-safe write to the in-memory submission store."""
    with _submissions_lock:
        _submissions[student_id] = {"answers": answers, "score": score}


def get_submissions_count() -> int:
    with _submissions_lock:
        return len(_submissions)


def flush_submissions(path: Path) -> None:
    """Optionally persist all submissions to a JSON file (call from shutdown hook)."""
    with _submissions_lock:
        data = dict(_submissions)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("[FLUSH] Submissions flushed to %s (%d records)", path, len(data))
    except OSError as exc:
        logger.error("[WARN] Could not flush submissions: %s", exc)
