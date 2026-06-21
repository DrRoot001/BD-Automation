import asyncio
from sqlalchemy import text
from app.database import engine

async def main():
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE candidates SET google_refresh_token = NULL"))
        print("Successfully reset google_refresh_token for all candidates in the database.")

if __name__ == "__main__":
    asyncio.run(main())
