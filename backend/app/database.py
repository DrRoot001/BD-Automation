from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
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
    pool_size=5,               # Base pool size (1 celery worker → 5 is plenty for testing)
    max_overflow=10,           # Allow burst connections beyond pool_size
)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
async_session_maker = AsyncSessionLocal

Base = declarative_base()


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