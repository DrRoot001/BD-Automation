"""Result / status types for the perception-driven autonomous agent."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AgentStatus(str, Enum):
    """Terminal outcome of an autonomous run.

    Every value is reached from OBSERVED evidence — never assumed. In
    particular SUBMITTED requires a positive confirmation observed AFTER the
    submit click (a fired submit alone is never treated as success).
    """
    SUBMITTED = "SUBMITTED"                       # positive confirmation observed
    FORM_COMPLETED = "FORM_COMPLETED"             # filled + submit fired, no confirmation yet / dry-run
    ABORTED = "ABORTED"                           # reasoner chose ABORT (wrong page, hard wall)
    WRONG_PAGE = "WRONG_PAGE"                     # never found an application surface
    MAX_STEPS = "MAX_STEPS"
    STUCK = "STUCK"                               # page stopped changing after mutating actions
    OTP_REQUIRED = "OTP_REQUIRED"                 # emailed one-time code screen, no handler
    EMAIL_VERIFICATION_REQUIRED = "EMAIL_VERIFICATION_REQUIRED"
    MFA_REQUIRED = "MFA_REQUIRED"                 # authenticator / 2FA / push
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"         # captcha wall, no solver handler
    DUPLICATE_ACCOUNT = "DUPLICATE_ACCOUNT"       # account exists / already applied
    NAVIGATION_FAILED = "NAVIGATION_FAILED"       # 404 / access denied / dead page
    ERROR = "ERROR"                               # internal error / repeated invalid reasoning

    @property
    def is_success(self) -> bool:
        return self in (AgentStatus.SUBMITTED, AgentStatus.FORM_COMPLETED)


@dataclass
class ActionResult:
    """Outcome of executing one action via Playwright."""
    ok: bool
    note: str = ""
    error: Optional[str] = None


@dataclass
class DetectedConditions:
    """What was OBSERVED on the page this turn (deterministic + reasoner-merged).

    Every flag is evidence-backed: the paired ``*_evidence`` string records the
    text/marker that justified it. This is the audit trail proving the agent
    *reasoned from evidence* rather than assuming workflow progression.
    """
    validation_failed: bool = False
    validation_evidence: str = ""
    completed_successfully: bool = False
    success_evidence: str = ""
    navigation_failed: bool = False
    navigation_evidence: str = ""
    otp_requested: bool = False
    otp_evidence: str = ""
    email_verification_requested: bool = False
    email_verification_evidence: str = ""
    mfa_requested: bool = False
    mfa_evidence: str = ""
    duplicate_account: bool = False
    duplicate_evidence: str = ""
    captcha_present: bool = False
    captcha_evidence: str = ""
    unexpected_ui: bool = False
    unexpected_ui_evidence: str = ""
    required_fields: List[str] = field(default_factory=list)

    # Conditions that should HALT the run when no handler can clear them.
    _BLOCKERS = (
        "otp_requested", "email_verification_requested", "mfa_requested",
        "duplicate_account", "captcha_present",
    )

    def merge(self, other: "DetectedConditions") -> None:
        """OR the boolean flags, keep the first evidence, take the latest
        required-field list. Used to accumulate across observe turns and to fold
        in the reasoner's own signal claims."""
        for f in (
            "validation_failed", "completed_successfully", "navigation_failed",
            "otp_requested", "email_verification_requested", "mfa_requested",
            "duplicate_account", "captcha_present", "unexpected_ui",
        ):
            if getattr(other, f):
                setattr(self, f, True)
                ev_attr = _EVIDENCE_ATTR.get(f)
                if ev_attr and not getattr(self, ev_attr) and getattr(other, ev_attr, ""):
                    setattr(self, ev_attr, getattr(other, ev_attr))
        if other.required_fields:
            self.required_fields = list(other.required_fields)


# flag -> its evidence attribute
_EVIDENCE_ATTR = {
    "validation_failed": "validation_evidence",
    "completed_successfully": "success_evidence",
    "navigation_failed": "navigation_evidence",
    "otp_requested": "otp_evidence",
    "email_verification_requested": "email_verification_evidence",
    "mfa_requested": "mfa_evidence",
    "duplicate_account": "duplicate_evidence",
    "captcha_present": "captcha_evidence",
    "unexpected_ui": "unexpected_ui_evidence",
}


@dataclass
class AgentRunResult:
    """Everything an autonomous run produced — status + evidence + history."""
    status: AgentStatus = AgentStatus.ERROR
    confirmation: Optional[str] = None
    error: Optional[str] = None
    steps_taken: int = 0
    # Accumulated observed conditions across the whole run.
    conditions: DetectedConditions = field(default_factory=DetectedConditions)
    # One entry per turn (the reasoning summary + the action + its result).
    history: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.status.is_success

    def to_dict(self) -> Dict[str, Any]:
        c = self.conditions
        return {
            "status": self.status.value,
            "confirmation": self.confirmation,
            "error": self.error,
            "steps_taken": self.steps_taken,
            "conditions": {
                "validation_failed": c.validation_failed,
                "completed_successfully": c.completed_successfully,
                "navigation_failed": c.navigation_failed,
                "otp_requested": c.otp_requested,
                "email_verification_requested": c.email_verification_requested,
                "mfa_requested": c.mfa_requested,
                "duplicate_account": c.duplicate_account,
                "captcha_present": c.captcha_present,
                "unexpected_ui": c.unexpected_ui,
                "required_fields": list(c.required_fields),
            },
            "history": self.history,
        }

    def summary(self) -> str:
        return (
            f"AgentRunResult(status={self.status.value} steps={self.steps_taken}"
            + (f" confirm={self.confirmation!r}" if self.confirmation else "")
            + (f" error={self.error!r}" if self.error else "")
            + ")"
        )
