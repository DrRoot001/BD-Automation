from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional, List
from uuid import UUID

class CandidateCreate(BaseModel):
    id: Optional[UUID] = None
    name: str
    email: str
    phone: Optional[str] = None
    location: Optional[str] = "US"
    work_auth: Optional[str] = "us_authorized"
    tech_stack: List[str] = []
    years_exp: Optional[int] = None
    linkedin_url: Optional[str] = None

class CandidateResponse(CandidateCreate):
    model_config = ConfigDict(from_attributes=True)
    
    id: UUID
    created_at: datetime
    updated_at: datetime