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

engine = create_async_engine(
    DATABASE_URL,
    # echo=True logs every SQL query — never enable in production
    echo=settings.environment == "development",
    future=True,
    pool_pre_ping=True,       # Verify connections before use (handles dropped TCP)
    pool_recycle=1800,         # Recycle connections after 30 min (prevents Supabase idle timeout)
    # Supabase PgBouncer (session mode) caps at 15 total connections.
    # FastAPI + Celery workers must share that budget. Keep FastAPI's pool
    # small so Celery task_session() calls (NullPool, 1 conn per task) have room.
    # Budget: FastAPI idle=3, FastAPI burst=+3 → max 6. Celery workers ~2-4. Total ≤ 10.
    pool_size=3,
    max_overflow=3,
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
    task_engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
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