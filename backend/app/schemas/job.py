from pydantic import BaseModel, ConfigDict, field_validator
from datetime import datetime
from typing import Optional, List
from uuid import UUID
import json


class JobCreate(BaseModel):
    title: str
    company: str
    location: Optional[str] = None
    source: str
    source_url: str
    canonical_url: Optional[str] = None
    description: Optional[str] = None
    skills: Optional[List[str]] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    pay_period: Optional[str] = None
    job_type: Optional[str] = None
    posted_at: Optional[datetime] = None
    embedding: Optional[List[float]] = None

    @field_validator('skills', mode='before')
    @classmethod
    def coerce_skills(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return []
        return v


class JobResponse(JobCreate):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_duplicate: Optional[bool] = False
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
