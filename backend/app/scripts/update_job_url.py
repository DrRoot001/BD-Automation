import asyncio
import os
import sys
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parent.parent
load_dotenv(dotenv_path=BACKEND_DIR / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

async def update():
    if not DATABASE_URL:
        print("DATABASE_URL not found!")
        return
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE jobs SET source_url = 'https://boards.greenhouse.io/monks/jobs/5996484004' WHERE id = '40a9f8a2-d257-47da-98e5-75dd30b34c20'")
        )
        print("Successfully updated job URL in DB!")
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(update())
