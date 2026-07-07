from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime
from typing import Optional, List
from uuid import UUID

class CandidateCreate(BaseModel):
    id: Optional[UUID] = None
    user_id: Optional[UUID] = None
    name: str
    email: str
    phone: Optional[str] = None
    location: Optional[str] = "US"
    work_auth: Optional[str] = "us_authorized"
    tech_stack: List[str] = []
    years_exp: Optional[int] = None
    linkedin_url: Optional[str] = None
    gmail: Optional[str] = None
    password: Optional[str] = None

class CandidateUpdate(BaseModel):
    user_id: Optional[UUID] = None
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    work_auth: Optional[str] = None
    tech_stack: Optional[List[str]] = None
    years_exp: Optional[int] = None
    linkedin_url: Optional[str] = None
    gmail: Optional[str] = None
    password: Optional[str] = None

class CandidateResponse(CandidateCreate):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
    google_connected: bool = False
    # SECURITY: never expose the stored portal password in API responses. It is
    # read server-side (browser-automation) straight from the DB; the frontend
    # must NOT receive it — and must not pre-fill its edit field with it, since
    # re-saving would then silently re-persist the old password. exclude=True
    # keeps it out of the serialized response while still allowing ORM load.
    password: Optional[str] = Field(default=None, exclude=True)