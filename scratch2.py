import asyncio
import sys, os
sys.path.insert(0, os.path.abspath('backend'))
from app.database import AsyncSessionLocal
from app.models.candidate import Candidate
from app.schemas.candidate import CandidateResponse
from sqlalchemy import select

async def run():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Candidate).where(Candidate.id == '76a9f624-ad21-41ac-bf10-fad936413b75'))
        c = result.scalar_one_or_none()
        try:
            resp = CandidateResponse.model_validate(c)
            print("Success:", resp)
        except Exception as e:
            print("Pydantic Error:", e)

asyncio.run(run())
