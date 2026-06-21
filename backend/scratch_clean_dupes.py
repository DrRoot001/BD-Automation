import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, delete
from app.models.candidate import Candidate
from app.config import get_settings

async def main():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        result = await session.execute(select(Candidate))
        candidates = result.scalars().all()
        
        # Group by a normalized name
        names = {}
        for c in candidates:
            # normalize name by removing trailing numbers if any, e.g. "Harmain Ali Butt 1781903036"
            import re
            norm_name = re.sub(r'\s+\d+$', '', c.name).strip() if c.name else "Unknown"
            if norm_name not in names:
                names[norm_name] = []
            names[norm_name].append(c)

        to_delete = []
        for norm_name, copies in names.items():
            if len(copies) > 1:
                # Sort by creation time, keep the oldest
                copies.sort(key=lambda x: x.created_at)
                # If there's a specific one with no '+' in email, prefer it
                preferred = next((x for x in copies if '+' not in x.email and not re.search(r'\.\d+@', x.email)), copies[0])
                
                for c in copies:
                    if c.id != preferred.id:
                        to_delete.append(c.id)
                        print(f"Deleting duplicate: {c.name} ({c.email})")
        
        if to_delete:
            await session.execute(delete(Candidate).where(Candidate.id.in_(to_delete)))
            await session.commit()
            print(f"Deleted {len(to_delete)} duplicates.")
        else:
            print("No duplicates by name found.")

if __name__ == "__main__":
    asyncio.run(main())
