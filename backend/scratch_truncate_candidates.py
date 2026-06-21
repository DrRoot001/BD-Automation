import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from app.config import get_settings

async def main():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        try:
            # This will truncate candidates and all dependent tables (resumes, applications, etc.)
            await session.execute(text("TRUNCATE TABLE candidates CASCADE;"))
            await session.commit()
            print("Successfully truncated candidates and all dependent tables.")
        except Exception as e:
            print(f"Failed to truncate candidates: {e}")
            await session.rollback()

if __name__ == "__main__":
    asyncio.run(main())
