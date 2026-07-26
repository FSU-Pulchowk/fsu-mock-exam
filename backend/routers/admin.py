"""
routers/admin.py — Secured admin dashboard API.

All endpoints require:
    Authorization: Bearer <ADMIN_TOKEN>

Endpoints
─────────
GET /admin/overview     Combined headline stats
GET /admin/health       RAM, CPU, disk, process memory, uptime
GET /admin/traffic      Per-minute request buckets (last 60 min)
GET /admin/submissions  Full paginated submission list
POST /admin/ping        Visitor ping from index.html (no auth — increments counter)
"""

import logging
import os
import time

import psutil
from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse

import cache
from config import ADMIN_TOKEN

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


# ── Public ping — no auth (increments visitor counter) ───────────────────────

@router.post("/ping", summary="Visitor ping from exam page")
async def visitor_ping():
    """
    Called by index.html on page load to register a visitor.
    No authentication required — this is a public lightweight endpoint.
    """
    cache.record_visit()
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

    return JSONResponse({
        "totalVisitors": cache.get_visit_count(),
        "visitorsLast60m": cache.get_recent_visits(60),
        "totalSubmissions": cache.get_submissions_count(),
        "setsLoaded": suffixes,
        "totalQuestions": total_questions,
        "uptimeSeconds": round(uptime_s, 1),
        "uptimeFormatted": uptime_str,
        "defaultSet": cache.get_default_suffix(),
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
        # getloadavg not available on Windows
        load_1 = load_5 = load_15 = None

    try:
        net = psutil.net_io_counters()
        network = {
            "bytesSent": net.bytes_sent,
            "bytesRecv": net.bytes_recv,
            "packetsSent": net.packets_sent,
            "packetsRecv": net.packets_recv,
        }
    except Exception:
        network = None

    return JSONResponse({
        "ram": {
            "totalMb": round(mem.total / 1024 / 1024, 1),
            "usedMb": round(mem.used / 1024 / 1024, 1),
            "availableMb": round(mem.available / 1024 / 1024, 1),
            "percentUsed": mem.percent,
        },
        "cpu": {
            "percentUsed": cpu_pct,
            "coreCount": psutil.cpu_count(logical=False),
            "threadCount": psutil.cpu_count(logical=True),
            "loadAvg": {"1m": load_1, "5m": load_5, "15m": load_15},
        },
        "disk": {
            "totalGb": round(disk.total / 1024 / 1024 / 1024, 1),
            "usedGb": round(disk.used / 1024 / 1024 / 1024, 1),
            "freeGb": round(disk.free / 1024 / 1024 / 1024, 1),
            "percentUsed": disk.percent,
        },
        "process": {
            "memoryMb": proc_mem_mb,
            "pid": os.getpid(),
        },
        "network": network,
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
    # Add human-readable label (HH:MM local)
    result = []
    for b in buckets:
        label = time.strftime("%H:%M", time.localtime(b["ts"]))
        result.append({"label": label, "count": b["count"], "errors": b["errors"]})
    return JSONResponse({"minutes": minutes, "buckets": result})


# ── Submissions ───────────────────────────────────────────────────────────────

@router.get("/submissions", summary="All exam submissions")
async def submissions(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=500),
    authorization: str | None = Header(default=None),
):
    _require_admin(authorization)

    all_subs = cache.get_all_submissions()
    # Sort newest first
    all_subs.sort(key=lambda x: x.get("ts", 0), reverse=True)

    total = len(all_subs)
    start = (page - 1) * per_page
    end = start + per_page
    page_data = all_subs[start:end]

    # Format timestamps
    for s in page_data:
        ts = s.get("ts", 0)
        s["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "—"

    return JSONResponse({
        "total": total,
        "page": page,
        "perPage": per_page,
        "totalPages": max(1, -(-total // per_page)),  # ceiling division
        "submissions": page_data,
    })
