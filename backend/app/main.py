"""BD Automator FastAPI application factory."""
import sys
import uuid
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler
import structlog
from app.logging_config import configure_logging

from app.config import get_settings
from app.routers import candidates, resumes, jobs, applications, analytics, auth, companies
from app.middleware.rate_limit import limiter

# Configure global structured logging
configure_logging()

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start / stop background services around the app lifecycle."""
    # Initialise Sentry early so any startup errors are captured
    _init_sentry()

    from app.routers.websocket import start_redis_subscriber, stop_redis_subscriber
    await start_redis_subscriber()
    
    # Run the watchdog once on startup to clean up any stale jobs from prior crashes
    import asyncio
    from app.database import AsyncSessionLocal
    from app.services.state_machine import recover_stuck_applications_async
    
    async def _run_startup_watchdog():
        try:
            async with AsyncSessionLocal() as session:
                await recover_stuck_applications_async(session)
        except Exception as e:
            logger.error(f"[Startup] Watchdog failed: {e}")
            
    asyncio.create_task(_run_startup_watchdog())
    
    yield
    await stop_redis_subscriber()


def _init_sentry() -> None:
    """Wire Sentry SDK if DSN is configured."""
    dsn = settings.sentry_dsn
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        sentry_sdk.init(
            dsn=dsn,
            integrations=[StarletteIntegration(), FastApiIntegration()],
            traces_sample_rate=0.1,
            environment=settings.environment,
            send_default_pii=False,
        )
        logger.info("[App] Sentry initialised (environment=%s)", settings.environment)
    except ImportError:
        logger.warning("[App] sentry-sdk not installed — error tracking disabled.")


def create_app() -> FastAPI:
    app = FastAPI(
        title="BD Automator API",
        description="FastAPI Central Orchestrator for BD Automator Agent",
        version="1.0.0",
        lifespan=lifespan,
        # Disable docs in production
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url="/redoc" if settings.environment != "production" else None,
    )

    # ── Rate limiter ─────────────────────────────────────────────────────────
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ── CORS ─────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Request correlation ID middleware ─────────────────────────────────────
    @app.middleware("http")
    async def add_correlation_id(request: Request, call_next):
        """Attach a unique X-Request-ID to every request/response for traceability."""
        rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        
        # Bind the request ID to all structlog logs for this request context
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid)
        
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    # ── Module 1 routers ──────────────────────────────────────────────────────
    app.include_router(auth.router)
    app.include_router(candidates.router)
    app.include_router(resumes.router)
    app.include_router(jobs.router)
    app.include_router(applications.router)
    app.include_router(analytics.router)
    app.include_router(companies.router)

    # ── Module 2 routes (optional — graceful if unavailable) ──────────────────
    try:
        from app.routers.module2_routes import router as module2_router
        app.include_router(module2_router)
    except Exception:
        pass

    # ── Module 5 — Dashboard API + WebSocket ──────────────────────────────────
    from app.routers.dashboard import router as dashboard_router
    from app.routers.websocket import router as ws_router
    app.include_router(dashboard_router)
    app.include_router(ws_router)

    # ── Health endpoints ──────────────────────────────────────────────────────
    @app.get("/api/health", tags=["health"])
    async def health_check():
        """Basic liveness probe — always returns 200 if the process is alive."""
        return {
            "status": "ok",
            "app_name": "BD Automator API",
            "version": "1.0.0",
            "environment": settings.environment,
        }

    @app.get("/api/health/ready", tags=["health"])
    async def readiness_check():
        """Readiness probe — verifies DB and Redis are reachable."""
        from sqlalchemy import text
        from app.database import AsyncSessionLocal
        from app.redis_client import redis_client

        checks: dict = {}
        overall = "ok"

        # Database
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = f"error: {exc}"
            overall = "degraded"

        # Redis
        try:
            await redis_client.ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = f"error: {exc}"
            overall = "degraded"

        status_code = 200 if overall == "ok" else 503
        return JSONResponse(
            content={"status": overall, "checks": checks},
            status_code=status_code,
        )

    # ── Global error handler ──────────────────────────────────────────────────
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        import traceback
        logger.error(
            "Unhandled exception: %s %s — %s",
            request.method,
            request.url.path,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error", "msg": str(exc)},
        )

    return app


app = create_app()