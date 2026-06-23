from pydantic import BaseModel
from typing import Literal, Optional, Dict

class ApplicationPackage(BaseModel):
    application_id: str
    candidate_id: str
    job_id: str
    job_title: Optional[str] = None
    job_description: Optional[str] = None
    job_url: str
    platform: str
    ats_type: Optional[str] = None
    # Hiring-company name. Used by the AgentLoop to anchor the AI on the actual
    # employer when answering screening questions like "Why this company?".
    company: Optional[str] = None
    resume_url: str
    cover_letter_url: Optional[str] = None
    # All keys the form filler may need: name, first_name, last_name, email, phone,
    # location, linkedin_url, website, experience_years, work_authorization,
    # sponsorship, agree_terms, etc.
    candidate_profile: Dict[str, str]
    screening_answers: Optional[Dict[str, str]] = None

class ApplicationResult(BaseModel):
    application_id: str
    status: Literal["SUBMITTED", "FORM_COMPLETED", "FAILED", "CAPTCHA_FAILED", "RATE_LIMITED", "BLOCKED"]
    screenshot_url: Optional[str] = None
    confirmation_text: Optional[str] = None
    error_message: Optional[str] = None
    execution_time_seconds: float
    retry_count: int
    platform_response: Optional[Dict] = None
