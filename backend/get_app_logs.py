import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.application import Application

async def get_logs():
    async with AsyncSessionLocal() as session:
        stmt = select(Application).where(Application.job_id == 'efd8d71c-c221-4944-9cdd-05f78e882f24')
        result = await session.execute(stmt)
        app = result.scalar_one_or_none()
        if app:
            print(f"Status: {app.status}")
            print(f"Resume ID: {app.resume_id}")
            print(f"Cover Letter ID: {app.cover_letter_url}")
        else:
            print("Application not found.")

if __name__ == "__main__":
    asyncio.run(get_logs())
