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
        
        emails = {}
        for c in candidates:
            if c.email not in emails:
                emails[c.email] = []
            emails[c.email].append(c)

        for email, copies in emails.items():
            if len(copies) > 1:
                print(f"Duplicate email: {email}")
                for c in copies:
                    print(f"  ID: {c.id}, Created: {c.created_at}")

if __name__ == "__main__":
    asyncio.run(main())
