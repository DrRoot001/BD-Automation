from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional
from uuid import UUID
from enum import Enum

class ApplicationStatus(str, Enum):
    FOUND = "FOUND"
    ANALYZED = "ANALYZED"
    MATCHED = "MATCHED"
    RESUME_UPDATED = "RESUME_UPDATED"
    COVER_LETTER_CREATED = "COVER_LETTER_CREATED"
    QUEUED = "QUEUED"
    APPLICATION_STARTED = "APPLICATION_STARTED"
    FORM_COMPLETED = "FORM_COMPLETED"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    INTERVIEW_R1 = "INTERVIEW_R1"
    INTERVIEW_R2 = "INTERVIEW_R2"
    REJECTED = "REJECTED"
    OFFER = "OFFER"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"

class StatusUpdateRequest(BaseModel):
    status: ApplicationStatus
    metadata: Optional[dict] = None
    resume_id: Optional[UUID] = None
    cover_letter_url: Optional[str] = None
    fit_score: Optional[float] = None
    ats_score: Optional[float] = None
    combined_score: Optional[float] = None

class ApplicationCreate(BaseModel):
    candidate_id: UUID
    job_id: UUID
    status: Optional[ApplicationStatus] = ApplicationStatus.FOUND
    resume_id: Optional[UUID] = None
    fit_score: Optional[float] = None
    ats_score: Optional[float] = None
    combined_score: Optional[float] = None

class ApplicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    candidate_id: UUID
    job_id: UUID
    resume_id: Optional[UUID] = None
    cover_letter_url: Optional[str] = None
    status: ApplicationStatus
    fit_score: Optional[float] = None
    ats_score: Optional[float] = None
    combined_score: Optional[float] = None
    screenshot_url: Optional[str] = None
    submitted_at: Optional[datetime] = None
    error_message: Optional[str] = None
    retry_count: Optional[int] = 0
    created_at: datetime
    updated_at: Optional[datetime] = None