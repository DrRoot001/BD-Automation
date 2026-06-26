"""Rate-limiting middleware helpers.

Usage in routers:
    from app.middleware.rate_limit import limiter
    from fastapi import Request

    @router.post("/some-endpoint")
    @limiter.limit("5/minute")
    async def my_endpoint(request: Request, ...):
        ...

The limiter is backed by Redis (same Upstash instance as Celery/pub-sub) so
limits are shared across all uvicorn workers / processes.
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address
from app.config import get_settings

settings = get_settings()

# Use Redis as the backend so rate counts are shared across workers.
# Falls back gracefully to in-memory if Redis URL is not set.
_storage_uri = settings.redis_url or "memory://"

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=_storage_uri,
    default_limits=[],   # No global default — limits applied per-endpoint
)
