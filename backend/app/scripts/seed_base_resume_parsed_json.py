"""
One-shot script: parse the base resume PDF and save parsed_json to DB.
Run from project root: python3 backend/app/scripts/seed_base_resume_parsed_json.py
"""
import asyncio
import os
import sys
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parent.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR.parent))

from dotenv import load_dotenv
load_dotenv(dotenv_path=str(BACKEND_DIR / ".env"))

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

CANDIDATE_ID = "3e8b9e17-755f-4c57-b165-83345c4d2b55"
RESUME_PDF = "/Users/sabihhaider/Documents/BD-Automator-Agent/Sabih Haider — Software Engineer _ Full-Stack Web Developer.pdf"

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)


async def seed():
    print(f"Parsing resume PDF: {RESUME_PDF}")
    from module3.parser.resume_parser import parse_resume
    resume_data = await parse_resume(RESUME_PDF, candidate_id=CANDIDATE_ID)
    parsed_json = resume_data.sections.model_dump()
    print(f"Parsed successfully. Skills found: {len(parsed_json.get('skills', []))}")

    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE resumes SET parsed_json = :pj "
                "WHERE candidate_id = :cid AND is_base = true AND parsed_json IS NULL "
                "RETURNING id"
            ),
            {"pj": json.dumps(parsed_json), "cid": CANDIDATE_ID}
        )
        rows = result.fetchall()
        if rows:
            print(f"Updated {len(rows)} base resume record(s): {[r.id for r in rows]}")
        else:
            print("No NULL parsed_json base resume found — already seeded or record missing.")
    await engine.dispose()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(seed())
