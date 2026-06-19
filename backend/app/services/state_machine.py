from fastapi import HTTPException
from app.schemas.application import ApplicationStatus

VALID_TRANSITIONS: dict[str, list[str]] = {
    "FOUND": ["ANALYZED"],
    "ANALYZED": ["MATCHED"],
    "MATCHED": ["RESUME_UPDATED"],
    "RESUME_UPDATED": ["COVER_LETTER_CREATED"],
    "COVER_LETTER_CREATED": ["QUEUED"],
    "QUEUED": ["APPLICATION_STARTED", "FORM_COMPLETED"],
    "APPLICATION_STARTED": ["FORM_COMPLETED", "QUEUED", "ANALYZED"],
    "FORM_COMPLETED": ["SUBMITTED", "QUEUED"],
    "SUBMITTED": ["CONFIRMED", "REJECTED"],
    "CONFIRMED": ["INTERVIEW_R1", "REJECTED"],
    "INTERVIEW_R1": ["INTERVIEW_R2", "REJECTED"],
    "INTERVIEW_R2": ["OFFER", "REJECTED"],
}

class InvalidTransitionError(Exception):
    pass

def validate_transition(current: str, target: str) -> bool:
    allowed = VALID_TRANSITIONS.get(current, [])
    if target not in allowed and target != current:
        raise InvalidTransitionError(
            f"Invalid transition: {current} → {target}. Allowed: {allowed}"
        )
    return True