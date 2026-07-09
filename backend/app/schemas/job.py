import json
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


def clean_url_field(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str):
        return v
    cleaned = v.strip()
    if 'not found' in cleaned.lower() or cleaned == '—' or cleaned == '':
        return None
    return cleaned


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
    ats_type: Optional[str] = None
    job_category: Optional[str] = None
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

    @field_validator('source_url', 'canonical_url', mode='before', check_fields=False)
    @classmethod
    def validate_urls(cls, v):
        return clean_url_field(v)


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    company: str
    location: Optional[str] = None
    source: str
    source_url: Optional[str] = None
    canonical_url: Optional[str] = None
    description: Optional[str] = None
    skills: Optional[List[str]] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    pay_period: Optional[str] = None
    job_type: Optional[str] = None
    posted_at: Optional[datetime] = None
    ats_type: Optional[str] = None
    job_category: Optional[str] = None
    is_duplicate: Optional[bool] = False
    duplicate_of: Optional[UUID] = None
    created_at: datetime

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

    @field_validator('source_url', 'canonical_url', mode='before', check_fields=False)
    @classmethod
    def validate_urls(cls, v):
        return clean_url_field(v)


class JobMatchingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    company: str
    location: Optional[str] = None
    source: str
    source_url: Optional[str] = None
    canonical_url: Optional[str] = None
    skills: Optional[List[str]] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    pay_period: Optional[str] = None
    job_type: Optional[str] = None
    posted_at: Optional[datetime] = None
    ats_type: Optional[str] = None
    job_category: Optional[str] = None
    created_at: datetime

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

    @field_validator('source_url', 'canonical_url', mode='before', check_fields=False)
    @classmethod
    def validate_urls(cls, v):
        return clean_url_field(v)

