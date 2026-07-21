"""
routers/health.py — Lightweight health-check and metrics endpoints.

These are used by:
  - Load balancers to decide whether to route traffic to this instance
  - Monitoring tools to track uptime and submission counts
"""

from fastapi import APIRouter
from models import HealthResponse
import cache

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Returns 200 OK with service status. Use this as your load-balancer probe.",
)
async def health_check() -> HealthResponse:
    return HealthResponse(
        status="ok",
        questions_loaded=cache.is_questions_loaded(),
        answers_loaded=cache.is_answers_loaded(),
        submissions_count=cache.get_submissions_count(),
    )


@router.get(
    "/",
    summary="Root",
    description="Quick sanity check.",
)
async def root():
    return {
        "service": "FSU Mock Exam API",
        "version": "2.0.0",
        "docs": "/docs",
        "health": "/health",
    }
