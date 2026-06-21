import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from app.models.candidate import Candidate
from app.config import get_settings

async def main():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        result = await session.execute(select(Candidate))
        candidates = result.scalars().all()
        for c in candidates:
            print(f"ID: {c.id}, Email: {c.email}, Name: {c.name}")

if __name__ == "__main__":
    asyncio.run(main())
