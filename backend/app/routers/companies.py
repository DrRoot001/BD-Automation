from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.company import Company

router = APIRouter(prefix="/api/companies", tags=["companies"])

@router.post("", status_code=201)
async def create_company(company: dict, db: AsyncSession = Depends(get_db)):
    db_company = Company(**company)
    db.add(db_company)
    await db.commit()
    await db.refresh(db_company)
    return db_company

@router.get("")
async def get_companies(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Company))
    return result.scalars().all()