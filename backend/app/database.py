from contextlib import asynccontextmanager
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import declarative_base
from fastapi import HTTPException
from app.config import get_settings

settings = get_settings()

# Explicitly use asyncpg driver
DATABASE_URL = settings.database_url
if not DATABASE_URL.startswith("postgresql+asyncpg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

import os as _os

# Supabase's session-mode pooler caps TOTAL clients at 15. The backend AND the
# Celery worker each create this engine in their own process, so the SUM of both
# pools' max connections must stay under 15 — otherwise the pooler rejects with
# "(EMAXCONNSESSION) max clients reached in session mode". The OLD config
# (pool_size=5 + max_overflow=10 = 15 PER PROCESS) blew past that the moment the
# dashboard fired its parallel queries while the worker held a connection.
# pool_size=4 + max_overflow=2 = 6 max/process → backend(6) + worker(6) = 12 < 15,
# leaving headroom for the NullPool task_session bursts. Override via env if your
# Supabase plan allows more clients.
_DB_POOL_SIZE = int(_os.getenv("DB_POOL_SIZE", "4"))
_DB_MAX_OVERFLOW = int(_os.getenv("DB_MAX_OVERFLOW", "2"))

engine = create_async_engine(
    DATABASE_URL,
    # echo=True logs every SQL query — never enable in production
    echo=settings.environment == "development",
    future=True,
    pool_pre_ping=True,       # Verify connections before use (handles dropped TCP)
    pool_recycle=1800,        # Recycle connections after 30 min (prevents Supabase idle timeout)
    pool_size=_DB_POOL_SIZE,
    max_overflow=_DB_MAX_OVERFLOW,
    pool_timeout=30,          # Wait up to 30s for a free connection instead of erroring under burst
    # PgBouncer transaction-mode (Supabase port 6543) does NOT support
    # asyncpg prepared statements — set cache size to 0 to disable them.
    connect_args={"statement_cache_size": 0},
)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
async_session_maker = AsyncSessionLocal

Base = declarative_base()


@asynccontextmanager
async def task_session():
    """
    Async DB session for Celery tasks.

    Uses NullPool so that connections are never cached across event loops.
    Celery workers call asyncio.run() / new_event_loop() for each task, which
    closes the previous loop.  A pooled asyncpg connection is bound to the loop
    that created it — reusing it in a new loop raises
    "Future attached to a different loop".  NullPool opens and closes a fresh
    connection for every session, completely sidestepping this.

    Secondary benefit: avoids EMAXCONNSESSION on PgBouncer — connections are
    released immediately after each session instead of sitting in a pool.
    """
    task_engine = create_async_engine(
        DATABASE_URL,
        poolclass=NullPool,
        # PgBouncer transaction-mode (Supabase port 6543) does NOT support
        # asyncpg prepared statements — must disable cache here too.
        connect_args={"statement_cache_size": 0},
    )
    try:
        maker = async_sessionmaker(task_engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            yield session
    finally:
        await task_engine.dispose()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    try:
        async with AsyncSessionLocal() as session:
            yield session
    except HTTPException as http_e:
        raise http_e
    except Exception as e:
        import sys, traceback
        print(f"GET_DB ERROR: {type(e)} {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        raise e