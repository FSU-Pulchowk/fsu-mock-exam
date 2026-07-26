"""
database.py — SQLite persistence layer.

All tables are created automatically with CREATE TABLE IF NOT EXISTS on first run.
SQLite is used as a write-through store alongside the in-memory cache.
The in-memory cache remains the fast hot path; SQLite provides persistence across
server restarts.

Database path: config.DB_PATH  (default: backend/data/fsu_exam.db)

WAL journal mode is enabled so concurrent reads never block writes.
"""

import sqlite3
import threading
import time
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


# ── Initialisation ────────────────────────────────────────────────────────────

def init_db(db_path: Path) -> None:
    """
    Open (or create) the SQLite database and ensure all tables exist.
    Safe to call multiple times — uses IF NOT EXISTS everywhere.
    """
    global _conn
    db_path.parent.mkdir(parents=True, exist_ok=True)

    _conn = sqlite3.connect(str(db_path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row

    # WAL mode: concurrent readers never block writers
    _conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL sync: safe on crash (OS buffer flush) but much faster than FULL
    _conn.execute("PRAGMA synchronous=NORMAL")
    # Increase cache for better read performance
    _conn.execute("PRAGMA cache_size=-8000")   # ~8 MB

    with _conn:
        _conn.executescript("""
            -- Candidate login sessions (historical — never pruned)
            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                email       TEXT    NOT NULL,
                set_suffix  TEXT    NOT NULL,
                ip          TEXT,
                created_at  REAL    NOT NULL
            );

            -- Exam submissions (one row per student_id; upserted on re-submit)
            CREATE TABLE IF NOT EXISTS submissions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id    TEXT    NOT NULL UNIQUE,
                student_name  TEXT,
                student_email TEXT,
                set_suffix    TEXT,
                score         REAL    NOT NULL DEFAULT 0,
                answered      INTEGER NOT NULL DEFAULT 0,
                submitted_at  REAL    NOT NULL
            );

            -- Full API traffic log (pruned after 48 h to keep DB small)
            CREATE TABLE IF NOT EXISTS traffic_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL    NOT NULL,
                endpoint     TEXT    NOT NULL,
                method       TEXT    NOT NULL,
                status_code  INTEGER NOT NULL,
                response_ms  REAL
            );

            -- Page visit log from /admin/ping (pruned after 48 h)
            CREATE TABLE IF NOT EXISTS visitor_log (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                ts   REAL NOT NULL,
                ip   TEXT
            );

            -- Indexes for time-range queries
            CREATE INDEX IF NOT EXISTS idx_traffic_ts  ON traffic_log(ts);
            CREATE INDEX IF NOT EXISTS idx_visitor_ts  ON visitor_log(ts);
            CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions(created_at);
            CREATE INDEX IF NOT EXISTS idx_subs_ts     ON submissions(submitted_at);
        """)

    logger.info("[DB] SQLite initialised at %s", db_path)


def _db() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("database.init_db() has not been called yet.")
    return _conn


# ── Writers ───────────────────────────────────────────────────────────────────

def insert_session(
    name: str,
    email: str,
    set_suffix: str,
    ip: str | None = None,
) -> None:
    """Persist a candidate login session."""
    with _lock:
        with _db() as conn:
            conn.execute(
                """INSERT INTO sessions (name, email, set_suffix, ip, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, email, set_suffix, ip, time.time()),
            )


def insert_submission(
    student_id: str,
    student_name: str | None,
    student_email: str | None,
    set_suffix: str | None,
    score: float,
    answered: int,
) -> None:
    """
    Persist or update an exam submission.
    Uses UPSERT so a re-submission replaces the old record (same student_id).
    """
    with _lock:
        with _db() as conn:
            conn.execute(
                """INSERT INTO submissions
                       (student_id, student_name, student_email, set_suffix,
                        score, answered, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(student_id) DO UPDATE SET
                       student_name  = excluded.student_name,
                       student_email = excluded.student_email,
                       set_suffix    = excluded.set_suffix,
                       score         = excluded.score,
                       answered      = excluded.answered,
                       submitted_at  = excluded.submitted_at
                """,
                (
                    student_id,
                    student_name or "",
                    student_email or "",
                    set_suffix or "",
                    score,
                    answered,
                    time.time(),
                ),
            )


def insert_traffic(
    endpoint: str,
    method: str,
    status_code: int,
    response_ms: float | None = None,
) -> None:
    """Record one API request into the traffic log."""
    with _lock:
        with _db() as conn:
            conn.execute(
                """INSERT INTO traffic_log (ts, endpoint, method, status_code, response_ms)
                   VALUES (?, ?, ?, ?, ?)""",
                (time.time(), endpoint, method, status_code, response_ms),
            )


def insert_visitor(ip: str | None = None) -> None:
    """Record a page visit from the exam frontend."""
    with _lock:
        with _db() as conn:
            conn.execute(
                "INSERT INTO visitor_log (ts, ip) VALUES (?, ?)",
                (time.time(), ip),
            )


# ── Req/sec & traffic analytics ───────────────────────────────────────────────

def get_req_per_sec(window_sec: int = 60) -> float:
    """Requests per second averaged over the last *window_sec* seconds."""
    cutoff = time.time() - window_sec
    row = _db().execute(
        "SELECT COUNT(*) FROM traffic_log WHERE ts >= ?", (cutoff,)
    ).fetchone()
    count = row[0] if row else 0
    return round(count / max(window_sec, 1), 2)


def get_req_per_sec_windows() -> dict[str, float]:
    """req/sec for 1 s, 5 s, 10 s, 60 s, and 5-minute windows."""
    now = time.time()
    result: dict[str, float] = {}
    for label, secs in [("1s", 1), ("5s", 5), ("10s", 10), ("60s", 60), ("5m", 300)]:
        cutoff = now - secs
        row = _db().execute(
            "SELECT COUNT(*) FROM traffic_log WHERE ts >= ?", (cutoff,)
        ).fetchone()
        count = row[0] if row else 0
        result[label] = round(count / secs, 2)
    return result


def get_error_rate(window_sec: int = 60) -> float:
    """Fraction of requests in last *window_sec* that returned 4xx/5xx."""
    cutoff = time.time() - window_sec
    conn = _db()
    total = conn.execute(
        "SELECT COUNT(*) FROM traffic_log WHERE ts >= ?", (cutoff,)
    ).fetchone()[0]
    if not total:
        return 0.0
    errors = conn.execute(
        "SELECT COUNT(*) FROM traffic_log WHERE ts >= ? AND status_code >= 400",
        (cutoff,),
    ).fetchone()[0]
    return round(errors / total, 4)


def get_avg_response_ms(window_sec: int = 60) -> float | None:
    """Average response latency (ms) over the last *window_sec* seconds."""
    cutoff = time.time() - window_sec
    row = _db().execute(
        """SELECT AVG(response_ms) FROM traffic_log
           WHERE ts >= ? AND response_ms IS NOT NULL""",
        (cutoff,),
    ).fetchone()
    val = row[0] if row else None
    return round(val, 1) if val is not None else None


def get_p95_response_ms(window_sec: int = 60) -> float | None:
    """95th-percentile response latency (ms) — proxy for tail latency."""
    cutoff = time.time() - window_sec
    rows = _db().execute(
        """SELECT response_ms FROM traffic_log
           WHERE ts >= ? AND response_ms IS NOT NULL
           ORDER BY response_ms""",
        (cutoff,),
    ).fetchall()
    if not rows:
        return None
    idx = int(len(rows) * 0.95)
    return round(rows[min(idx, len(rows) - 1)][0], 1)


def get_top_endpoints(window_sec: int = 60, limit: int = 10) -> list[dict]:
    """Top endpoints by hit count over the last *window_sec* seconds."""
    cutoff = time.time() - window_sec
    rows = _db().execute(
        """SELECT endpoint, method,
                  COUNT(*) AS count,
                  SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS errors,
                  ROUND(AVG(response_ms), 1) AS avg_ms
           FROM traffic_log
           WHERE ts >= ?
           GROUP BY endpoint, method
           ORDER BY count DESC
           LIMIT ?""",
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Aggregates ────────────────────────────────────────────────────────────────

def get_submission_count() -> int:
    return _db().execute("SELECT COUNT(*) FROM submissions").fetchone()[0]


def get_session_count() -> int:
    return _db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


def get_visitor_count() -> int:
    return _db().execute("SELECT COUNT(*) FROM visitor_log").fetchone()[0]


def get_recent_visitor_count(minutes: int = 60) -> int:
    cutoff = time.time() - minutes * 60
    return _db().execute(
        "SELECT COUNT(*) FROM visitor_log WHERE ts >= ?", (cutoff,)
    ).fetchone()[0]


def get_active_session_count(minutes: int = 30) -> int:
    """Sessions created in the last *minutes* minutes — proxy for concurrent users."""
    cutoff = time.time() - minutes * 60
    return _db().execute(
        "SELECT COUNT(*) FROM sessions WHERE created_at >= ?", (cutoff,)
    ).fetchone()[0]


# ── Full record readers (for admin tables) ───────────────────────────────────

def get_all_submissions() -> list[dict]:
    """All submissions from DB, newest first, formatted for admin table."""
    rows = _db().execute(
        "SELECT * FROM submissions ORDER BY submitted_at DESC"
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["time"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(d.get("submitted_at", 0))
        )
        d["ts"]           = d.pop("submitted_at", 0)
        d["studentId"]    = d.pop("student_id", "")
        d["studentName"]  = d.pop("student_name", "")
        d["studentEmail"] = d.pop("student_email", "")
        d["setSuffix"]    = d.pop("set_suffix", "")
        result.append(d)
    return result


def get_all_sessions() -> list[dict]:
    """All sessions from DB, newest first, formatted for admin table."""
    rows = _db().execute(
        "SELECT * FROM sessions ORDER BY created_at DESC"
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["name"]      = d.get("name", "")
        d["email"]     = d.get("email", "")
        d["set"]       = d.pop("set_suffix", "")
        d["loginTime"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(d.get("created_at", 0))
        )
        d["ageMinutes"] = round(
            (time.time() - d.get("created_at", time.time())) / 60, 1
        )
        result.append(d)
    return result


# ── Risk / capacity assessment ────────────────────────────────────────────────

# Conservative per-worker throughput estimates (req/sec)
# Lower bound: endpoints that hit SQLite (submit, login)
# Upper bound: fully in-memory endpoints (getQuestions from cache)
_CAPACITY_PER_WORKER_RPS = 150   # mixed workload conservative estimate


def assess_capacity(
    workers: int,
    ram_pct: float,
    cpu_pct: float,
) -> dict:
    """
    Compute a risk score and human-readable flags for the current load.

    Risk levels:
      LOW      — server is healthy, plenty of headroom
      MODERATE — noticeable load, monitor closely
      HIGH     — approaching limits, degradation possible
      CRITICAL — server may become unresponsive, take action now
    """
    rps_now  = get_req_per_sec(window_sec=10)   # short window = most current
    rps_60s  = get_req_per_sec(window_sec=60)
    rps_windows = get_req_per_sec_windows()
    error_rate  = get_error_rate(window_sec=60)
    avg_ms      = get_avg_response_ms(window_sec=60)
    p95_ms      = get_p95_response_ms(window_sec=60)
    active_sess = get_active_session_count(minutes=30)

    capacity_rps = _CAPACITY_PER_WORKER_RPS * max(workers, 1)
    rps_ratio    = rps_now / max(capacity_rps, 1)

    # ── Scoring (additive, 0–100) ─────────────────────────────────────────────
    score  = 0
    flags: list[str] = []
    recommendations: list[str] = []

    # req/sec vs capacity
    if rps_ratio >= 0.9:
        score += 40
        flags.append(
            f"Request rate ({rps_now} rps) is at {int(rps_ratio*100)}% of estimated capacity "
            f"({capacity_rps} rps for {workers} worker(s))."
        )
        recommendations.append("Scale up workers or add a caching layer immediately.")
    elif rps_ratio >= 0.7:
        score += 25
        flags.append(
            f"Request rate ({rps_now} rps) is at {int(rps_ratio*100)}% of capacity — "
            "headroom is shrinking."
        )
        recommendations.append("Consider scaling workers or enabling rate limiting.")
    elif rps_ratio >= 0.4:
        score += 10
        flags.append(f"Moderate traffic: {rps_now} rps (~{int(rps_ratio*100)}% of capacity).")

    # Error rate
    if error_rate > 0.15:
        score += 35
        flags.append(f"Critical error rate: {error_rate*100:.1f}% of requests are failing.")
        recommendations.append("Check application logs immediately — high error rate detected.")
    elif error_rate > 0.05:
        score += 20
        flags.append(f"Elevated error rate: {error_rate*100:.1f}% of requests returned 4xx/5xx.")
        recommendations.append("Investigate error logs; rate limiting may be triggering.")
    elif error_rate > 0.01:
        score += 5
        flags.append(f"Minor errors: {error_rate*100:.1f}% error rate (within tolerance).")

    # RAM
    if ram_pct >= 92:
        score += 25
        flags.append(f"RAM critically high: {ram_pct}% used. OOM risk imminent.")
        recommendations.append("Free memory or add swap. Restart workers if needed.")
    elif ram_pct >= 80:
        score += 15
        flags.append(f"RAM usage elevated: {ram_pct}%.")
        recommendations.append("Monitor memory — approaching limits.")
    elif ram_pct >= 65:
        score += 5

    # CPU
    if cpu_pct >= 90:
        score += 25
        flags.append(f"CPU critically high: {cpu_pct}%. Requests will queue.")
        recommendations.append("Add more workers or reduce CPU-heavy operations.")
    elif cpu_pct >= 75:
        score += 15
        flags.append(f"CPU usage elevated: {cpu_pct}%.")
    elif cpu_pct >= 50:
        score += 5

    # Avg response time
    if avg_ms is not None:
        if avg_ms > 2000:
            score += 15
            flags.append(f"Very high avg response time: {avg_ms} ms. Users will notice delays.")
            recommendations.append("Profiling needed — something is blocking the event loop.")
        elif avg_ms > 500:
            score += 8
            flags.append(f"Elevated avg response time: {avg_ms} ms.")

    # Active sessions
    if active_sess > capacity_rps * 30:
        flags.append(
            f"{active_sess} sessions in the last 30 min — could spike to "
            f"~{int(active_sess/30)} rps if all submit simultaneously."
        )

    # Clamp score to 0–100
    score = min(score, 100)

    if score >= 65:
        level = "CRITICAL"
    elif score >= 40:
        level = "HIGH"
    elif score >= 18:
        level = "MODERATE"
    else:
        level = "LOW"

    if not flags:
        flags.append("All systems nominal.")
    if not recommendations:
        recommendations.append("No action required — server is healthy.")

    # Concurrent user estimate (based on exam pattern: ~1 req / 20 s per user)
    max_concurrent_safe = int(capacity_rps * 20 * 0.7)   # 70% headroom

    return {
        "riskLevel": level,
        "score": score,
        "flags": flags,
        "recommendations": recommendations,
        "metrics": {
            "reqPerSec": rps_windows,
            "reqPerSecNow": rps_now,
            "reqPerSec60s": rps_60s,
            "errorRate": error_rate,
            "errorRatePct": round(error_rate * 100, 2),
            "avgResponseMs": avg_ms,
            "p95ResponseMs": p95_ms,
            "capacityRps": capacity_rps,
            "rpsRatioPct": round(rps_ratio * 100, 1),
            "activeSessionsLast30m": active_sess,
            "maxConcurrentUsersSafe": max_concurrent_safe,
            "workers": workers,
        },
    }


# ── Maintenance ───────────────────────────────────────────────────────────────

def prune_old_logs(max_age_hours: int = 48) -> dict[str, int]:
    """
    Delete traffic_log and visitor_log rows older than *max_age_hours*.
    Call periodically (e.g. every hour) to keep the DB compact.
    Returns counts of deleted rows per table.
    """
    cutoff = time.time() - max_age_hours * 3600
    deleted: dict[str, int] = {}
    with _lock:
        with _db() as conn:
            cur = conn.execute(
                "DELETE FROM traffic_log WHERE ts < ?", (cutoff,)
            )
            deleted["traffic_log"] = cur.rowcount
            cur = conn.execute(
                "DELETE FROM visitor_log WHERE ts < ?", (cutoff,)
            )
            deleted["visitor_log"] = cur.rowcount
    if any(deleted.values()):
        logger.info("[DB] Pruned old logs: %s", deleted)
    return deleted
