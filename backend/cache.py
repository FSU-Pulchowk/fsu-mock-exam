"""
cache.py — In-memory data store loaded once at application startup.
"""

import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

# ── Module-level singletons ───────────────────────────────────────────────────

# { suffix: { ...questions payload... } }
_questions_store: dict[str, dict[str, Any]] = {}
# { suffix: { ...answers payload... } }
_answers_store: dict[str, dict[str, Any]] = {}

# Submission store: { studentId: { "answers": {...}, "score": float, "ts": float, "answered": int } }
_submissions: dict[str, dict] = {}
_submissions_lock = Lock()

_default_suffix: str | None = None

# ── Telemetry stores ──────────────────────────────────────────────────────────

# Timestamps of each unique exam session start (/getQuestions hit)
_visit_log: deque[float] = deque(maxlen=50_000)
_visit_lock = Lock()

# Full API traffic log: (timestamp, endpoint, method, status_code)
_traffic_log: deque[dict] = deque(maxlen=100_000)
_traffic_lock = Lock()

# Process start time for uptime calculation
_start_time: float = time.time()


# ── Telemetry functions ───────────────────────────────────────────────────────

def record_visit() -> None:
    """Record a unique exam page visit (called from /getQuestions)."""
    with _visit_lock:
        _visit_log.append(time.time())


def record_traffic(endpoint: str, method: str, status_code: int) -> None:
    """Record an API request for the traffic timeline."""
    with _traffic_lock:
        _traffic_log.append({
            "ts": time.time(),
            "endpoint": endpoint,
            "method": method,
            "status": status_code,
        })


def get_visit_count() -> int:
    with _visit_lock:
        return len(_visit_log)


def get_recent_visits(minutes: int = 60) -> int:
    """Return visit count in the last N minutes."""
    cutoff = time.time() - minutes * 60
    with _visit_lock:
        return sum(1 for ts in _visit_log if ts >= cutoff)


def get_traffic_buckets(minutes: int = 60) -> list[dict]:
    """
    Return per-minute request counts for the last *minutes* minutes.
    Each bucket: { label: "HH:MM", count: int, errors: int }
    """
    now = time.time()
    cutoff = now - minutes * 60
    # Build minute-keyed buckets
    buckets: dict[int, dict] = {}
    for i in range(minutes):
        bucket_ts = int((now - (minutes - 1 - i) * 60) // 60) * 60
        buckets[bucket_ts] = {"ts": bucket_ts, "count": 0, "errors": 0}

    with _traffic_lock:
        for entry in _traffic_log:
            if entry["ts"] < cutoff:
                continue
            bucket_key = int(entry["ts"] // 60) * 60
            if bucket_key in buckets:
                buckets[bucket_key]["count"] += 1
                if entry["status"] >= 400:
                    buckets[bucket_key]["errors"] += 1

    return sorted(buckets.values(), key=lambda b: b["ts"])


def get_uptime_seconds() -> float:
    return time.time() - _start_time


def get_all_submissions() -> list[dict]:
    """Return a list of submission records for the admin table."""
    with _submissions_lock:
        return [
            {
                "studentId": sid,
                "score": data.get("score", 0),
                "answered": data.get("answered", 0),
                "ts": data.get("ts", 0),
            }
            for sid, data in _submissions.items()
        ]


def load_all_sets(data_dir: Path) -> list[str]:
    """
    Scan *data_dir* for matching ``sets_<suffix>.json`` /
    ``answers_<suffix>.json`` pairs and load them all into memory.

    Returns the list of loaded suffixes (e.g. ``["i", "ii"]``).
    Missing answer files for a question-set are tolerated (answers will be
    ``None`` for that suffix).
    """
    global _default_suffix

    if not data_dir.is_dir():
        logger.error("[CACHE] DATA_DIR does not exist: %s", data_dir)
        return []

    # Discover all sets_*.json files
    sets_pattern = re.compile(r"^sets_(.+)\.json$", re.IGNORECASE)
    suffixes: list[str] = []

    for f in sorted(data_dir.iterdir()):
        m = sets_pattern.match(f.name)
        if m:
            suffixes.append(m.group(1))

    if not suffixes:
        logger.warning("[CACHE] No sets_*.json files found in %s", data_dir)
        return []

    for suffix in suffixes:
        q_path = data_dir / f"sets_{suffix}.json"
        a_path = data_dir / f"answers_{suffix}.json"

        # Load questions
        try:
            with open(q_path, "r", encoding="utf-8") as fh:
                _questions_store[suffix] = json.load(fh)
            logger.info(
                "[CACHE] Set '%s' questions loaded — %d questions from '%s'",
                suffix,
                len(_questions_store[suffix].get("questions", [])),
                q_path.name,
            )
        except FileNotFoundError:
            logger.error("[CACHE] Questions file not found: %s", q_path)
        except json.JSONDecodeError as exc:
            logger.error("[CACHE] Invalid JSON in %s: %s", q_path.name, exc)

        # Load answers (optional — warn only)
        if a_path.exists():
            try:
                with open(a_path, "r", encoding="utf-8") as fh:
                    _answers_store[suffix] = json.load(fh)
                logger.info(
                    "[CACHE] Set '%s' answers loaded — %d answers from '%s'",
                    suffix,
                    len(_answers_store[suffix].get("answers", [])),
                    a_path.name,
                )
            except json.JSONDecodeError as exc:
                logger.error("[CACHE] Invalid JSON in %s: %s", a_path.name, exc)
        else:
            logger.warning("[CACHE] No answers file for set '%s' (expected %s)", suffix, a_path.name)

    # Set default suffix to first successfully loaded set
    loaded = [s for s in suffixes if s in _questions_store]
    if loaded and _default_suffix is None:
        _default_suffix = loaded[0]
        logger.info("[CACHE] Default set suffix: '%s'", _default_suffix)

    return loaded


# ── Legacy single-file loaders (backwards compatibility) ─────────────────────

def load_questions(path: Path) -> None:
    """Load a single questions file into the ``'_legacy'`` slot."""
    global _default_suffix
    try:
        with open(path, "r", encoding="utf-8") as f:
            _questions_store["_legacy"] = json.load(f)
        logger.info(
            "[CACHE] (legacy) Questions loaded — %d questions from '%s'",
            len(_questions_store["_legacy"].get("questions", [])),
            path.name,
        )
        if _default_suffix is None:
            _default_suffix = "_legacy"
    except FileNotFoundError:
        logger.error("[ERROR] Questions file not found: %s", path)
    except json.JSONDecodeError as exc:
        logger.error("[ERROR] Invalid JSON in questions file: %s", exc)


def load_answers(path: Path) -> None:
    """Load a single answers file into the ``'_legacy'`` slot."""
    global _default_suffix
    try:
        with open(path, "r", encoding="utf-8") as f:
            _answers_store["_legacy"] = json.load(f)
        logger.info(
            "[CACHE] (legacy) Answers loaded — %d answers from '%s'",
            len(_answers_store["_legacy"].get("answers", [])),
            path.name,
        )
        if _default_suffix is None:
            _default_suffix = "_legacy"
    except FileNotFoundError:
        logger.error("[ERROR] Answers file not found: %s", path)
    except json.JSONDecodeError as exc:
        logger.error("[ERROR] Invalid JSON in answers file: %s", exc)


# ── Accessors ─────────────────────────────────────────────────────────────────

def get_questions(suffix: str | None = None) -> dict[str, Any] | None:
    """Return questions for *suffix*, or the default set when suffix is None."""
    key = suffix if suffix is not None else _default_suffix
    return _questions_store.get(key) if key else None


def get_answers(suffix: str | None = None) -> dict[str, Any] | None:
    """Return answers for *suffix*, or the default set when suffix is None."""
    key = suffix if suffix is not None else _default_suffix
    return _answers_store.get(key) if key else None


def get_all_suffixes() -> list[str]:
    """Return all loaded set suffixes (excluding the legacy sentinel)."""
    return [s for s in _questions_store if s != "_legacy"]


def get_default_suffix() -> str | None:
    return _default_suffix


def is_questions_loaded(suffix: str | None = None) -> bool:
    return get_questions(suffix) is not None


def is_answers_loaded(suffix: str | None = None) -> bool:
    return get_answers(suffix) is not None


# ── Submission store ──────────────────────────────────────────────────────────

def store_submission(student_id: str, answers: dict, score: float) -> None:
    """Thread-safe write to the in-memory submission store."""
    with _submissions_lock:
        _submissions[student_id] = {
            "answers": answers,
            "score": score,
            "answered": len(answers),
            "ts": time.time(),
        }


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
