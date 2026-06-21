import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, delete
from app.models.candidate import Candidate
from app.models.resume import Resume
from app.models.application import Application
from app.models.application_history import ApplicationHistory
from app.models.email import Email
from app.models.interview import Interview
from app.config import get_settings

async def main():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        result = await session.execute(select(Candidate))
        candidates = result.scalars().all()
        
        names = {}
        for c in candidates:
            import re
            norm_name = re.sub(r'\s+\d+$', '', c.name).strip() if c.name else "Unknown"
            if norm_name not in names:
                names[norm_name] = []
            names[norm_name].append(c)

        to_delete_cands = []
        for norm_name, copies in names.items():
            if len(copies) > 1:
                copies.sort(key=lambda x: x.created_at)
                preferred = next((x for x in copies if '+' not in x.email and not re.search(r'\.\d+@', x.email)), copies[0])
                for c in copies:
                    if c.id != preferred.id:
                        to_delete_cands.append(c.id)
                        print(f"Queueing deletion for duplicate: {c.name} ({c.email})")
        
        if to_delete_cands:
            # First find applications for these candidates
            result = await session.execute(select(Application.id).where(Application.candidate_id.in_(to_delete_cands)))
            app_ids = result.scalars().all()
            
            if app_ids:
                # Delete Application History
                await session.execute(delete(ApplicationHistory).where(ApplicationHistory.application_id.in_(app_ids)))
                # Delete Interviews
                await session.execute(delete(Interview).where(Interview.application_id.in_(app_ids)))
                # Delete Emails
                await session.execute(delete(Email).where(Email.application_id.in_(app_ids)))
                # Delete Applications
                await session.execute(delete(Application).where(Application.id.in_(app_ids)))
                
            # Delete Resumes
            await session.execute(delete(Resume).where(Resume.candidate_id.in_(to_delete_cands)))
            
            # Delete Candidates
            await session.execute(delete(Candidate).where(Candidate.id.in_(to_delete_cands)))
            
            await session.commit()
            print(f"Successfully deleted {len(to_delete_cands)} duplicate candidates and their related data.")
        else:
            print("No duplicates by name found.")

if __name__ == "__main__":
    asyncio.run(main())
