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

async def check():
    if not DATABASE_URL:
        print("DATABASE_URL not found!")
        return
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT id, title, company, source, source_url FROM jobs LIMIT 20"))
        rows = res.fetchall()
        print("--- JOBS IN DATABASE ---")
        for row in rows:
            print(f"ID: {row.id} | Title: {row.title} | Company: {row.company} | Source: {row.source} | URL: {row.source_url}")
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(check())
