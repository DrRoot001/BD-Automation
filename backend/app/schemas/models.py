from enum import Enum
from typing import List, Dict, Optional, Any
from pydantic import BaseModel, Field, EmailStr, HttpUrl
from datetime import datetime
from uuid import UUID

# --- Enums ---

class ApplicationStatus(str, Enum):
    FOUND = 'FOUND'
    ANALYZED = 'ANALYZED'
    MATCHED = 'MATCHED'
    RESUME_UPDATED = 'RESUME_UPDATED'
    COVER_LETTER_CREATED = 'COVER_LETTER_CREATED'
    QUEUED = 'QUEUED'
    APPLICATION_STARTED = 'APPLICATION_STARTED'
    FORM_COMPLETED = 'FORM_COMPLETED'
    SUBMITTED = 'SUBMITTED'
    CONFIRMED = 'CONFIRMED'
    INTERVIEW_R1 = 'INTERVIEW_R1'
    INTERVIEW_R2 = 'INTERVIEW_R2'
    REJECTED = 'REJECTED'
    OFFER = 'OFFER'
    FAILED = 'FAILED'
    BLOCKED = 'BLOCKED'

class EmailClassification(str, Enum):
    APPLIED_CONFIRMATION = 'APPLIED_CONFIRMATION'
    INTERVIEW_R1 = 'INTERVIEW_R1'
    INTERVIEW_R2 = 'INTERVIEW_R2'
    ASSESSMENT = 'ASSESSMENT'
    REJECTED = 'REJECTED'
    OFFER = 'OFFER'
    UNKNOWN = 'UNKNOWN'

# --- State Machine Configuration & Validator ---

VALID_TRANSITIONS: Dict[ApplicationStatus, List[ApplicationStatus]] = {
    ApplicationStatus.FOUND: [ApplicationStatus.ANALYZED, ApplicationStatus.FAILED],
    ApplicationStatus.ANALYZED: [ApplicationStatus.MATCHED, ApplicationStatus.APPLICATION_STARTED, ApplicationStatus.FAILED],
    ApplicationStatus.MATCHED: [ApplicationStatus.RESUME_UPDATED, ApplicationStatus.APPLICATION_STARTED, ApplicationStatus.QUEUED, ApplicationStatus.FAILED],
    ApplicationStatus.RESUME_UPDATED: [ApplicationStatus.COVER_LETTER_CREATED, ApplicationStatus.FORM_COMPLETED, ApplicationStatus.APPLICATION_STARTED, ApplicationStatus.QUEUED, ApplicationStatus.FAILED],
    ApplicationStatus.COVER_LETTER_CREATED: [ApplicationStatus.QUEUED, ApplicationStatus.FAILED],
    ApplicationStatus.QUEUED: [ApplicationStatus.APPLICATION_STARTED, ApplicationStatus.FORM_COMPLETED, ApplicationStatus.FAILED],
    ApplicationStatus.APPLICATION_STARTED: [ApplicationStatus.FORM_COMPLETED, ApplicationStatus.QUEUED, ApplicationStatus.ANALYZED, ApplicationStatus.FAILED, ApplicationStatus.BLOCKED],
    ApplicationStatus.FORM_COMPLETED: [ApplicationStatus.SUBMITTED, ApplicationStatus.QUEUED, ApplicationStatus.FAILED],
    ApplicationStatus.SUBMITTED: [ApplicationStatus.CONFIRMED, ApplicationStatus.REJECTED],
    ApplicationStatus.CONFIRMED: [ApplicationStatus.INTERVIEW_R1, ApplicationStatus.REJECTED],
    ApplicationStatus.INTERVIEW_R1: [ApplicationStatus.INTERVIEW_R2, ApplicationStatus.REJECTED],
    ApplicationStatus.INTERVIEW_R2: [ApplicationStatus.OFFER, ApplicationStatus.REJECTED],
    ApplicationStatus.FAILED: [],
    ApplicationStatus.BLOCKED: [ApplicationStatus.QUEUED],
    ApplicationStatus.REJECTED: [],
    ApplicationStatus.OFFER: [],
}

class InvalidTransitionException(Exception):
    """Raised when an invalid status transition is attempted."""
    pass

def validate_transition(current: ApplicationStatus, target: ApplicationStatus) -> bool:
    if current == target:
        return True
    allowed = VALID_TRANSITIONS.get(current, [])
    if target in allowed:
        return True
    raise InvalidTransitionException(f"Invalid transition from {current} to {target}")

# --- Pydantic Data Models (Base and Interface Schemas) ---

class CandidateBase(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    location: str = "US"
    work_auth: str = "us_authorized"
    tech_stack: List[str] = []
    years_exp: Optional[int] = None
    linkedin_url: Optional[str] = None


class Candidate(CandidateBase):
    id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True

class CompanyBase(BaseModel):
    name: str
    domain: Optional[str] = None
    ats_type: Optional[str] = None
    rate_limit_config: Optional[Dict[str, Any]] = None

class Company(CompanyBase):
    id: UUID
    created_at: datetime

    class Config:
        orm_mode = True

class JobBase(BaseModel):
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

class Job(JobBase):
    id: UUID
    is_duplicate: bool = False
    duplicate_of: Optional[UUID] = None
    created_at: datetime

    class Config:
        orm_mode = True

class ResumeBase(BaseModel):
    candidate_id: UUID
    version: int
    file_url: str
    parsed_json: Optional[Dict[str, Any]] = None
    is_base: bool = False
    tailored_for_job_id: Optional[UUID] = None

class Resume(ResumeBase):
    id: UUID
    created_at: datetime

    class Config:
        orm_mode = True

class ApplicationBase(BaseModel):
    candidate_id: UUID
    job_id: UUID
    resume_id: Optional[UUID] = None
    cover_letter_url: Optional[str] = None
    status: ApplicationStatus = ApplicationStatus.FOUND
    fit_score: Optional[float] = None
    ats_score: Optional[float] = None
    combined_score: Optional[float] = None
    screenshot_url: Optional[str] = None
    submitted_at: Optional[datetime] = None
    error_message: Optional[str] = None
    retry_count: int = 0

class Application(ApplicationBase):
    id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True
