from fastapi import HTTPException
from app.schemas.application import ApplicationStatus

VALID_TRANSITIONS: dict[str, list[str]] = {
    "FOUND":               ["ANALYZED", "FAILED"],
    "ANALYZED":            ["MATCHED", "APPLICATION_STARTED", "FAILED"],
    "MATCHED":             ["RESUME_UPDATED", "APPLICATION_STARTED", "QUEUED", "FAILED"],
    "RESUME_UPDATED":      ["COVER_LETTER_CREATED", "FORM_COMPLETED", "APPLICATION_STARTED", "QUEUED", "FAILED"],
    "COVER_LETTER_CREATED":["QUEUED", "FAILED"],
    "QUEUED":              ["APPLICATION_STARTED", "FORM_COMPLETED", "FAILED"],
    "APPLICATION_STARTED": ["FORM_COMPLETED", "QUEUED", "ANALYZED", "FAILED", "BLOCKED"],
    "FORM_COMPLETED":      ["SUBMITTED", "QUEUED", "FAILED"],
    "SUBMITTED":           ["CONFIRMED", "REJECTED", "GHOSTED"],
    "CONFIRMED":           ["INTERVIEW_R1", "REJECTED", "WITHDRAWN"],
    "INTERVIEW_R1":        ["INTERVIEW_R2", "REJECTED", "WITHDRAWN", "OFFER"],
    "INTERVIEW_R2":        ["OFFER", "REJECTED", "WITHDRAWN"],
    # Terminal or pseudo-terminal states
    "FAILED":              [],
    "BLOCKED":             ["QUEUED"],   # Blocked apps can be retried
    "REJECTED":            [],
    "OFFER":               [],
    "GHOSTED":             ["QUEUED"],   # Can retry if desired
    "WITHDRAWN":           [],           # Terminal
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