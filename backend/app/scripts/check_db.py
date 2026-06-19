import asyncio
from sqlalchemy import select
from app.database import get_db
from app.models.candidate import Candidate

async def check():
    try:
        # get_db is an async generator, we get the session
        async for session in get_db():
            result = await session.execute(select(Candidate).limit(1))
            row = result.scalars().first()
            print("Successfully connected to Supabase!")
            print("Candidate table query result:", row)
            break
    except Exception as e:
        print("Database verification failed:", e)

if __name__ == "__main__":
    asyncio.run(check())
