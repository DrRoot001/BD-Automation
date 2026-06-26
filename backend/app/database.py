from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.config import get_settings

settings = get_settings()

# Explicitly use asyncpg driver
DATABASE_URL = settings.database_url
if not DATABASE_URL.startswith("postgresql+asyncpg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

engine = create_async_engine(
    DATABASE_URL, 
    echo=True, 
    future=True,
    pool_pre_ping=True,
    pool_recycle=1800
)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    try:
        async with AsyncSessionLocal() as session:
            yield session
    except Exception as e:
        import sys, traceback
        print(f"GET_DB ERROR: {type(e)} {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        raise e