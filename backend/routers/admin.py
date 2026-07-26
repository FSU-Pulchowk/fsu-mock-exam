"""
routers/admin.py — Secured admin dashboard API.

All endpoints require:
    Authorization: Bearer <ADMIN_TOKEN>

Endpoints
─────────
GET  /admin/overview      Combined headline stats (uses SQLite for persistent counts)
GET  /admin/health        RAM, CPU, disk, process memory, uptime
GET  /admin/traffic       Per-minute request buckets (last 60 min)
GET  /admin/submissions   All exam submissions from SQLite (persistent)
GET  /admin/sessions      All candidate login sessions from SQLite (persistent)
GET  /admin/capacity      Real-time req/sec, risk assessment, concurrent user estimate
POST /admin/ping          Visitor ping from index.html (no auth)
"""

import logging
import os
import time

import psutil
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import cache
import database
from config import ADMIN_TOKEN, WORKERS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"])

_PROCESS = psutil.Process(os.getpid())


# ── Auth dependency ───────────────────────────────────────────────────────────

def _require_admin(authorization: str | None = Header(default=None)) -> None:
    """Raise 401/403 if the request does not carry a valid admin token."""
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Admin access is not configured on this server.")
    if authorization is None or authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing admin token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Public ping — no auth ─────────────────────────────────────────────────────

@router.post("/ping", summary="Visitor ping from exam page")
async def visitor_ping(request: Request):
    """
    Called by index.html on page load to register a visitor.
    No authentication required.
    """
    cache.record_visit()
    try:
        ip = request.client.host if request.client else None
        database.insert_visitor(ip=ip)
    except Exception:
        pass
    return {"ok": True}


# ── Overview ──────────────────────────────────────────────────────────────────

@router.get("/overview", summary="Headline statistics")
async def overview(authorization: str | None = Header(default=None)):
    _require_admin(authorization)

    suffixes = cache.get_all_suffixes()
    total_questions = sum(
        len((cache.get_questions(s) or {}).get("questions", []))
        for s in suffixes
    )

    uptime_s = cache.get_uptime_seconds()
    hours, rem = divmod(int(uptime_s), 3600)
    minutes, secs = divmod(rem, 60)
    uptime_str = f"{hours:02d}h {minutes:02d}m {secs:02d}s"

    # Use DB for persistent counts
    total_visitors    = database.get_visitor_count()
    visitors_last_60m = database.get_recent_visitor_count(60)
    total_submissions = database.get_submission_count()
    total_sessions    = database.get_session_count()

    # req/sec windows
    rps_windows = database.get_req_per_sec_windows()

    return JSONResponse({
        "totalVisitors":    total_visitors,
        "visitorsLast60m":  visitors_last_60m,
        "totalSubmissions": total_submissions,
        "totalSessions":    total_sessions,
        "setsLoaded":       suffixes,
        "totalQuestions":   total_questions,
        "uptimeSeconds":    round(uptime_s, 1),
        "uptimeFormatted":  uptime_str,
        "defaultSet":       cache.get_default_suffix(),
        "reqPerSec":        rps_windows,
    })


# ── Health ────────────────────────────────────────────────────────────────────

@router.get("/health", summary="Server system health")
async def health(authorization: str | None = Header(default=None)):
    _require_admin(authorization)

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    cpu_pct = psutil.cpu_percent(interval=0.3)

    try:
        proc_mem_mb = round(_PROCESS.memory_info().rss / 1024 / 1024, 1)
    except Exception:
        proc_mem_mb = 0.0

    try:
        load_1, load_5, load_15 = [round(x, 2) for x in psutil.getloadavg()]
    except AttributeError:
        load_1 = load_5 = load_15 = None

    try:
        net = psutil.net_io_counters()
        network = {
            "bytesSent":   net.bytes_sent,
            "bytesRecv":   net.bytes_recv,
            "packetsSent": net.packets_sent,
            "packetsRecv": net.packets_recv,
        }
    except Exception:
        network = None

    return JSONResponse({
        "ram": {
            "totalMb":     round(mem.total     / 1024 / 1024, 1),
            "usedMb":      round(mem.used      / 1024 / 1024, 1),
            "availableMb": round(mem.available / 1024 / 1024, 1),
            "percentUsed": mem.percent,
        },
        "cpu": {
            "percentUsed": cpu_pct,
            "coreCount":   psutil.cpu_count(logical=False),
            "threadCount": psutil.cpu_count(logical=True),
            "loadAvg":     {"1m": load_1, "5m": load_5, "15m": load_15},
        },
        "disk": {
            "totalGb":    round(disk.total / 1024 / 1024 / 1024, 1),
            "usedGb":     round(disk.used  / 1024 / 1024 / 1024, 1),
            "freeGb":     round(disk.free  / 1024 / 1024 / 1024, 1),
            "percentUsed": disk.percent,
        },
        "process": {
            "memoryMb": proc_mem_mb,
            "pid":      os.getpid(),
        },
        "network":    network,
        "serverTime": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    })


# ── Traffic ───────────────────────────────────────────────────────────────────

@router.get("/traffic", summary="Per-minute traffic timeline")
async def traffic(
    minutes: int = Query(default=60, ge=5, le=1440),
    authorization: str | None = Header(default=None),
):
    _require_admin(authorization)
    buckets = cache.get_traffic_buckets(minutes)
    result = []
    for b in buckets:
        label = time.strftime("%H:%M", time.localtime(b["ts"]))
        result.append({"label": label, "count": b["count"], "errors": b["errors"]})
    return JSONResponse({"minutes": minutes, "buckets": result})


# ── Submissions ───────────────────────────────────────────────────────────────

@router.get("/submissions", summary="All exam submissions (persistent)")
async def submissions(
    page:     int = Query(default=1,  ge=1),
    per_page: int = Query(default=50, ge=1, le=500),
    authorization: str | None = Header(default=None),
):
    _require_admin(authorization)

    # Query from SQLite — persists across server restarts
    all_subs = database.get_all_submissions()
    total    = len(all_subs)
    start    = (page - 1) * per_page
    page_data = all_subs[start : start + per_page]

    return JSONResponse({
        "total":      total,
        "page":       page,
        "perPage":    per_page,
        "totalPages": max(1, -(-total // per_page)),
        "submissions": page_data,
    })


# ── Active / Historical Sessions ──────────────────────────────────────────────

@router.get("/sessions", summary="Candidate login sessions (persistent)")
async def sessions(authorization: str | None = Header(default=None)):
    _require_admin(authorization)

    session_list = database.get_all_sessions()
    return JSONResponse({"total": len(session_list), "sessions": session_list})


# ── Capacity & Risk Assessment ────────────────────────────────────────────────

@router.get("/capacity", summary="Real-time capacity and concurrency risk assessment")
async def capacity(authorization: str | None = Header(default=None)):
    """
    Returns current req/sec metrics, a risk level (LOW/MODERATE/HIGH/CRITICAL),
    and human-readable flags describing what is stressing the server.

    The risk score is computed from:
      - Current req/sec vs estimated server capacity
      - Error rate in the last 60 s
      - RAM usage %
      - CPU usage %
      - Average and p95 response latency
      - Number of active candidate sessions (proxy for concurrent users)

    Use this endpoint to judge whether concurrent users could bring down the server.
    """
    _require_admin(authorization)

    mem     = psutil.virtual_memory()
    cpu_pct = psutil.cpu_percent(interval=0.2)

    assessment = database.assess_capacity(
        workers=WORKERS,
        ram_pct=mem.percent,
        cpu_pct=cpu_pct,
    )

    # Add top endpoints breakdown
    assessment["topEndpoints"] = database.get_top_endpoints(window_sec=60, limit=8)

    return JSONResponse(assessment)
