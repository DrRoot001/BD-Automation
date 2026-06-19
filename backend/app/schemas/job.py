from pydantic import BaseModel, ConfigDict, field_validator
from datetime import datetime
from typing import Optional, List
from uuid import UUID

class JobCreate(BaseModel):
    title: str
    company: str
    location: Optional[str] = None
    source: str
    source_url: str
    canonical_url: Optional[str] = None
    description: Optional[str] = None
    skills: List[str] = []
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    pay_period: Optional[str] = None
    job_type: Optional[str] = None
    posted_at: Optional[datetime] = None
    embedding: Optional[List[float]] = None  # Changed from [] to None

class JobResponse(JobCreate):
    model_config = ConfigDict(from_attributes=True)
    
    id: UUID
    is_duplicate: bool = False
    duplicate_of: Optional[UUID] = None
    created_at: datetime

    @field_validator('embedding', mode='before')
    @classmethod
    def convert_embedding(cls, v):
        if v is None:
            return None
        if hasattr(v, 'tolist'):
            return v.tolist()
        return v