"""
main.py — FastAPI application factory.

Architecture overview
─────────────────────
  ┌─────────────────────────────────────────────────────────┐
  │  Nginx / Caddy (optional reverse proxy, TLS termination) │
  └───────────────────────────┬───────────────────────────────┘
                              │ HTTP/1.1 or HTTP/2
  ┌───────────────────────────▼───────────────────────────────┐
  │  Gunicorn (process manager)                              │
  │    Worker 1  ─┐                                          │
  │    Worker 2  ─┤  each is a Uvicorn ASGI worker           │
  │    Worker N  ─┘  using uvloop (epoll on Linux)           │
  └───────────────────────────┬───────────────────────────────┘
                              │
  ┌───────────────────────────▼───────────────────────────────┐
  │  FastAPI app (this file)                                 │
  │    Middleware: CORS, SlowAPI rate limiter                 │
  │    Routers:   /getQuestions  /getAnswers                  │
  │               /submitAnswers /examInfo                    │
  │               /health        /                            │
  │    Cache:     ALL sets_*.json / answers_*.json loaded once │
  │               at startup and served from RAM              │
  └───────────────────────────────────────────────────────────┘

Memory profile (2.3 GB budget, 5K typical / 20K peak users)
────────────────────────────────────────────────────────────
  6 workers × 65 MB   = 390 MB
  uvloop connections  = ~8 KB × 20K = 160 MB
  in-memory cache     = ~3 MB
  submissions store   = ~6 MB (20K × 300 B)
  OS + overhead       = ~200 MB
  ─────────────────────────────
  Total               ≈ 760 MB   (well under 2.3 GB)
"""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import cache
import config
from middleware.rate_limit import limiter
from routers import exam as exam_router
from routers import health as health_router
from routers import admin as admin_router

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ── Lifespan (startup / shutdown hooks) ──────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load ALL sets_*/answers_* data pairs into memory at startup; flush submissions at shutdown."""
    logger.info("[STARTUP] FSU Exam API starting up ...")
    logger.info("   Data directory : %s", config.DATA_DIR)
    logger.info("   Rate limit     : %s", config.RATE_LIMIT)
    logger.info("   CORS origins   : %s", config.CORS_ORIGINS)

    loaded_suffixes = cache.load_all_sets(config.DATA_DIR)

    if config.QUESTIONS_FILE:
        logger.info("   [legacy] QUESTIONS_FILE override: %s", config.QUESTIONS_FILE)
        cache.load_questions(config.QUESTIONS_FILE)
    if config.ANSWERS_FILE:
        logger.info("   [legacy] ANSWERS_FILE override  : %s", config.ANSWERS_FILE)
        cache.load_answers(config.ANSWERS_FILE)

    if loaded_suffixes:
        logger.info("[STARTUP] Loaded %d set(s): %s", len(loaded_suffixes), loaded_suffixes)
    else:
        logger.warning("[WARN] No question sets loaded — /getQuestions will return 503")

    if not cache.is_answers_loaded():
        logger.warning("[WARN] No answer sets loaded — /getAnswers will return 503")

    logger.info("[STARTUP] Complete. Listening on %s:%d", config.HOST, config.PORT)
    yield  # ← application runs here

    # Shutdown
    logger.info("[SHUTDOWN] Shutting down -- flushing submissions ...")
    flush_path = config.BASE_DIR / "submissions.json"
    cache.flush_submissions(flush_path)
    logger.info("[SHUTDOWN] Complete.")


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="FSU Mock Exam Portal API",
        description=(
            "High-performance backend for the FSU IOE entrance mock exam portal. "
            "Serves 1K–20K concurrent users within a 2.3 GB RAM budget using "
            "FastAPI + Uvicorn async I/O with in-memory question caching."
        ),
        version="2.0.0",
        lifespan=lifespan,
        root_path=config.ROOT_PATH,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ── Traffic telemetry middleware ────────────────────────────────────────
    @app.middleware("http")
    async def _track_traffic(request: Request, call_next):
        response = await call_next(request)
        # Skip tracking for admin pings to avoid noise
        path = request.url.path
        cache.record_traffic(path, request.method, response.status_code)
        return response

    # ── Rate limiter ──────────────────────────────────────────────────────────
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ── CORS ──────────────────────────────────────────────────────────────────
    # In production: replace "*" with your frontend's exact origin, e.g.
    # ["https://exam.fsu.edu.np"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS if config.CORS_ORIGINS != ["*"] else ["*"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(exam_router.router)
    app.include_router(health_router.router)
    app.include_router(admin_router.router)

    # ── Global error handler ──────────────────────────────────────────────────
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal server error occurred. Please try again."},
        )

    return app


# ── Entry point for `uvicorn backend.main:app` ────────────────────────────────
app = create_app()
