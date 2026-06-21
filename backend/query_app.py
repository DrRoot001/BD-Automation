import asyncio
import sys
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

DATABASE_URL = "postgresql+asyncpg://postgres.rdfnydteruigmajebxsm:JOWA1pMxYz70t1YR@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres"

async def main():
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT id, status, screenshot_url, cover_letter_url, error_message, retry_count FROM applications WHERE id = '036eda83-946d-45f7-af0d-f87dd977ae74'"))
        row = result.fetchone()
        print(row)
        
        # also get history
        result2 = await conn.execute(text("SELECT from_status, to_status, created_at, meta_data FROM application_history WHERE application_id = '036eda83-946d-45f7-af0d-f87dd977ae74' ORDER BY created_at ASC"))
        for r in result2:
            print(r)

asyncio.run(main())
