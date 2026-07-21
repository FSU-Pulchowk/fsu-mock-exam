"""
middleware/rate_limit.py — slowapi rate limiter setup.

slowapi is a Starlette-compatible port of Flask-Limiter that works natively
with FastAPI.  It uses the client's IP address as the rate-limit key by
default, which is correct for exam traffic.

Default: 120 requests/minute per IP (configurable via RATE_LIMIT env var).
This allows a student to poll the exam UI fast without getting blocked,
while preventing flood attacks.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from config import RATE_LIMIT  # e.g. "120/minute"

# Single Limiter instance shared across the app
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[RATE_LIMIT],
    headers_enabled=True,   # adds X-RateLimit-* headers to responses
)
