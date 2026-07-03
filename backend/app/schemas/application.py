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
    INTERVIEW_R3 = "INTERVIEW_R3"
    INTERVIEW_R4 = "INTERVIEW_R4"
    REJECTED = "REJECTED"
    OFFER = "OFFER"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    GHOSTED = "GHOSTED"
    WITHDRAWN = "WITHDRAWN"

class FailureReason(str, Enum):
    INFRA_ERROR = "INFRA_ERROR"
    QUALIFICATION_MISMATCH = "QUALIFICATION_MISMATCH"
    BOT_DETECTED = "BOT_DETECTED"
    FORM_INCOMPLETE = "FORM_INCOMPLETE"
    JOB_EXPIRED = "JOB_EXPIRED"
    ROBOTS_BLOCKED = "ROBOTS_BLOCKED"
    # Form was submitted to the ATS but the post-submit email-verification code
    # could not be completed (Gmail not connected / code didn't arrive in time).
    EMAIL_VERIFICATION = "EMAIL_VERIFICATION"
    # ATS requires an account login we don't have credentials for.
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    # ATS rejected the submission as spam / flagged automation post-submit.
    SPAM_FLAGGED = "SPAM_FLAGGED"
    # ATS reports the candidate already applied to this job — never retry.
    ALREADY_APPLIED = "ALREADY_APPLIED"
    # Verification code needed but the candidate has no Gmail connection.
    GMAIL_NOT_CONNECTED = "GMAIL_NOT_CONNECTED"

class StatusUpdateRequest(BaseModel):
    status: ApplicationStatus
    metadata: Optional[dict] = None
    resume_id: Optional[UUID] = None
    cover_letter_url: Optional[str] = None
    fit_score: Optional[float] = None
    ats_score: Optional[float] = None
    combined_score: Optional[float] = None
    failure_reason: Optional[FailureReason] = None

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
    failure_reason: Optional[FailureReason] = None
    retry_count: Optional[int] = 0
    created_at: datetime
    updated_at: Optional[datetime] = None