"""Structured reasoning schema — the typed output the LLM must produce.

The decision layer replaces assumption-based rules ("after submit the only thing
left is an OTP screen", "no OTP wall ⇒ success", "country is always US") with
**evidence-grounded reasoning**: on every turn the model receives the current
:class:`~..perception.BrowserState` (screenshot + structured DOM), the objective,
and memory, and must answer a fixed set of questions — each answer justified by
something it actually observed.

This module is the *schema* only (dataclasses + parsing + structural
validation). Prompt assembly lives in :mod:`.prompt`; the orchestration that
calls the LLM lives in :mod:`.engine`.

The required questions map to fields:

    What page am I on?              → observation.page_type / page_evidence
    What changed?                   → observation.what_changed / change_evidence
    Did the previous action succeed?→ previous_action.succeeded / evidence
    Is there an error?              → signals.error
    Is OTP actually requested?      → signals.otp_requested
    Is email verification requested?→ signals.email_verification_requested
    Is CAPTCHA present?             → signals.captcha_present (+ type)
    What is the next action?        → action
    Why?                            → why  (must cite evidence)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class PageType(str, Enum):
    FORM = "FORM"
    LISTING = "LISTING"
    LOGIN = "LOGIN"
    VERIFICATION = "VERIFICATION"   # OTP / email-code / "check your inbox"
    CAPTCHA = "CAPTCHA"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    BLOCKED = "BLOCKED"             # bot wall / WAF / access denied
    LOADING = "LOADING"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def coerce(cls, v: Any) -> "PageType":
        try:
            return cls(str(v).strip().upper())
        except Exception:
            return cls.UNKNOWN


class ActionType(str, Enum):
    OBSERVE = "OBSERVE"                 # take another look (no DOM change)
    WAIT = "WAIT"                       # pause for async work
    SCROLL = "SCROLL"                   # {direction}
    CLICK = "CLICK"                     # {selector, value? as text}
    CLICK_APPLY = "CLICK_APPLY"         # {selector?} reveal the form
    FILL = "FILL"                       # {selector, value, field_label?}
    SELECT_OPTION = "SELECT_OPTION"     # {selector, value}
    UPLOAD = "UPLOAD"                   # {selector, value: resume|cover_letter}
    NEXT_STEP = "NEXT_STEP"             # click Next/Continue
    NAVIGATE = "NAVIGATE"               # {url}
    SOLVE_CAPTCHA = "SOLVE_CAPTCHA"     # {captcha_type}
    HANDLE_VERIFICATION = "HANDLE_VERIFICATION"  # runner fetches the emailed code
    SUBMIT = "SUBMIT"                   # {selector?}
    DONE = "DONE"                       # {confirmation}
    ABORT = "ABORT"                     # {reason}

    @classmethod
    def coerce(cls, v: Any) -> Optional["ActionType"]:
        try:
            return cls(str(v).strip().upper())
        except Exception:
            return None


# Action kinds that end the run.
_TERMINAL_ACTIONS = {ActionType.DONE, ActionType.ABORT}
# Action kinds that mutate the page (used by callers to reason about expected change).
_MUTATING_ACTIONS = {
    ActionType.CLICK, ActionType.CLICK_APPLY, ActionType.FILL,
    ActionType.SELECT_OPTION, ActionType.UPLOAD, ActionType.NEXT_STEP,
    ActionType.NAVIGATE, ActionType.SUBMIT,
}


# ─────────────────────────────────────────────────────────────────────────────
# Reasoning sub-structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SignalClaim:
    """A yes/no claim about the page that MUST carry observed evidence.

    ``detail`` holds a qualifier (e.g. the captcha type, the error text). A claim
    with ``present=True`` and no ``evidence`` is structurally suspect and gets a
    warning attached by the parser/validator.
    """
    present: bool = False
    evidence: str = ""
    detail: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "SignalClaim":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            present=bool(raw.get("present")),
            evidence=str(raw.get("evidence") or "").strip()[:400],
            detail=str(raw.get("detail") or raw.get("type") or "").strip()[:120],
        )


@dataclass
class Observation:
    page_type: PageType = PageType.UNKNOWN
    page_evidence: str = ""
    what_changed: str = ""
    change_evidence: str = ""


@dataclass
class PreviousActionAssessment:
    had_previous_action: bool = False
    succeeded: Optional[bool] = None    # None = cannot tell from evidence
    evidence: str = ""


@dataclass
class Signals:
    error: SignalClaim = field(default_factory=SignalClaim)
    otp_requested: SignalClaim = field(default_factory=SignalClaim)
    email_verification_requested: SignalClaim = field(default_factory=SignalClaim)
    captcha_present: SignalClaim = field(default_factory=SignalClaim)
    # Broader auth challenges beyond a one-time email code.
    mfa_requested: SignalClaim = field(default_factory=SignalClaim)     # authenticator/2FA/push
    duplicate_account: SignalClaim = field(default_factory=SignalClaim)  # "account already exists" / "already applied"


@dataclass
class NextAction:
    type: ActionType = ActionType.OBSERVE
    selector: Optional[str] = None
    value: Optional[str] = None
    field_label: Optional[str] = None
    url: Optional[str] = None
    direction: Optional[str] = None
    captcha_type: Optional[str] = None
    confirmation: Optional[str] = None
    reason: Optional[str] = None

    @classmethod
    def from_raw(cls, raw: Any) -> "NextAction":
        if not isinstance(raw, dict):
            return cls(type=ActionType.OBSERVE)
        atype = ActionType.coerce(raw.get("type")) or ActionType.OBSERVE
        return cls(
            type=atype,
            selector=(raw.get("selector") or None),
            value=(raw.get("value") if raw.get("value") is not None else None),
            field_label=(raw.get("field_label") or None),
            url=(raw.get("url") or None),
            direction=(raw.get("direction") or None),
            captcha_type=(raw.get("captcha_type") or None),
            confirmation=(raw.get("confirmation") or None),
            reason=(raw.get("reason") or None),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Top-level reasoning output
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReasoningOutput:
    """One fully-parsed, structurally-validated reasoning turn."""
    observation: Observation = field(default_factory=Observation)
    previous_action: PreviousActionAssessment = field(default_factory=PreviousActionAssessment)
    signals: Signals = field(default_factory=Signals)
    action: NextAction = field(default_factory=NextAction)
    why: str = ""
    confidence: float = 0.0

    # Populated by the parser/engine — never by the LLM.
    warnings: List[str] = field(default_factory=list)
    valid: bool = True                  # False ⇒ action not safe to execute as-is
    parse_error: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    # ── convenience ──
    @property
    def terminal(self) -> bool:
        return self.action.type in _TERMINAL_ACTIONS

    @property
    def is_mutating(self) -> bool:
        return self.action.type in _MUTATING_ACTIONS

    @property
    def needs_captcha(self) -> bool:
        return self.signals.captcha_present.present or self.action.type == ActionType.SOLVE_CAPTCHA

    @property
    def needs_verification(self) -> bool:
        return (
            self.signals.otp_requested.present
            or self.signals.email_verification_requested.present
            or self.action.type == ActionType.HANDLE_VERIFICATION
        )

    @property
    def needs_mfa(self) -> bool:
        return self.signals.mfa_requested.present

    @property
    def duplicate_account_detected(self) -> bool:
        return self.signals.duplicate_account.present

    @property
    def previous_failed(self) -> bool:
        return self.previous_action.succeeded is False

    # ── parsing ──
    @classmethod
    def from_raw(cls, raw: Any) -> "ReasoningOutput":
        """Parse a raw LLM dict into a typed output, defensively.

        Missing/renamed keys degrade to defaults rather than raising, and
        structural problems are recorded in ``warnings`` (the engine adds
        grounding warnings that need the BrowserState). Structural *validity*
        (``valid``) is decided by :meth:`validate_structure`.
        """
        out = cls()
        if not isinstance(raw, dict):
            out.parse_error = f"expected dict, got {type(raw).__name__}"
            out.valid = False
            return out
        out.raw = raw

        obs = raw.get("observation") or {}
        out.observation = Observation(
            page_type=PageType.coerce(obs.get("page_type")),
            page_evidence=str(obs.get("page_evidence") or "").strip()[:400],
            what_changed=str(obs.get("what_changed") or "").strip()[:400],
            change_evidence=str(obs.get("change_evidence") or "").strip()[:400],
        )

        prev = raw.get("previous_action_assessment") or raw.get("previous_action") or {}
        succ = prev.get("succeeded")
        out.previous_action = PreviousActionAssessment(
            had_previous_action=bool(prev.get("had_previous_action")),
            succeeded=(None if succ is None else bool(succ)),
            evidence=str(prev.get("evidence") or "").strip()[:400],
        )

        sig = raw.get("signals") or {}
        out.signals = Signals(
            error=SignalClaim.from_raw(sig.get("error")),
            otp_requested=SignalClaim.from_raw(sig.get("otp_requested")),
            email_verification_requested=SignalClaim.from_raw(
                sig.get("email_verification_requested")
            ),
            captcha_present=SignalClaim.from_raw(sig.get("captcha_present")),
            mfa_requested=SignalClaim.from_raw(sig.get("mfa_requested")),
            duplicate_account=SignalClaim.from_raw(sig.get("duplicate_account")),
        )

        dec = raw.get("decision") or {}
        out.action = NextAction.from_raw(dec.get("action") or raw.get("action"))
        out.why = str(dec.get("why") or raw.get("why") or "").strip()[:800]
        try:
            out.confidence = max(0.0, min(1.0, float(dec.get("confidence", raw.get("confidence", 0.0)))))
        except (TypeError, ValueError):
            out.confidence = 0.0

        out.validate_structure()
        return out

    # ── structural validation (no BrowserState needed) ──
    def validate_structure(self) -> None:
        """Check the action carries the params its type requires, and that
        boolean signals set ``present=True`` carry evidence. Appends warnings;
        sets ``valid=False`` when the action can't be executed as emitted."""
        a = self.action
        w = self.warnings

        def need(cond: bool, msg: str) -> None:
            if not cond:
                w.append(msg)
                self.valid = False

        if a.type == ActionType.FILL:
            need(bool(a.selector), "FILL missing selector")
            need(a.value is not None and a.value != "", "FILL missing value")
        elif a.type == ActionType.SELECT_OPTION:
            need(bool(a.selector), "SELECT_OPTION missing selector")
            need(a.value is not None and a.value != "", "SELECT_OPTION missing value")
        elif a.type == ActionType.UPLOAD:
            need(bool(a.selector), "UPLOAD missing selector")
            need(str(a.value or "").lower() in ("resume", "cover_letter"),
                 "UPLOAD value must be 'resume' or 'cover_letter'")
        elif a.type == ActionType.CLICK:
            need(bool(a.selector or a.value), "CLICK missing selector/text")
        elif a.type == ActionType.NAVIGATE:
            need(bool(a.url), "NAVIGATE missing url")
        elif a.type == ActionType.SOLVE_CAPTCHA:
            need(bool(a.captcha_type), "SOLVE_CAPTCHA missing captcha_type")
        elif a.type == ActionType.SCROLL:
            if a.direction not in ("up", "down"):
                w.append("SCROLL direction defaulted to 'down'")
                a.direction = "down"
        elif a.type == ActionType.ABORT:
            need(bool(a.reason), "ABORT missing reason")

        # Evidence discipline: a positive signal with no evidence is unsupported.
        for name, claim in (
            ("error", self.signals.error),
            ("otp_requested", self.signals.otp_requested),
            ("email_verification_requested", self.signals.email_verification_requested),
            ("captcha_present", self.signals.captcha_present),
            ("mfa_requested", self.signals.mfa_requested),
            ("duplicate_account", self.signals.duplicate_account),
        ):
            if claim.present and not claim.evidence:
                w.append(f"signal '{name}' claimed present with no evidence")

        # The 'why' must justify the action.
        if not self.why and a.type not in (ActionType.OBSERVE, ActionType.WAIT):
            w.append("action has no 'why' justification")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "observation": {
                "page_type": self.observation.page_type.value,
                "page_evidence": self.observation.page_evidence,
                "what_changed": self.observation.what_changed,
                "change_evidence": self.observation.change_evidence,
            },
            "previous_action": {
                "had_previous_action": self.previous_action.had_previous_action,
                "succeeded": self.previous_action.succeeded,
                "evidence": self.previous_action.evidence,
            },
            "signals": {
                k: {"present": c.present, "evidence": c.evidence, "detail": c.detail}
                for k, c in (
                    ("error", self.signals.error),
                    ("otp_requested", self.signals.otp_requested),
                    ("email_verification_requested", self.signals.email_verification_requested),
                    ("captcha_present", self.signals.captcha_present),
                    ("mfa_requested", self.signals.mfa_requested),
                    ("duplicate_account", self.signals.duplicate_account),
                )
            },
            "action": {
                "type": self.action.type.value,
                "selector": self.action.selector,
                "value": self.action.value,
                "field_label": self.action.field_label,
                "url": self.action.url,
                "direction": self.action.direction,
                "captcha_type": self.action.captcha_type,
                "confirmation": self.action.confirmation,
                "reason": self.action.reason,
            },
            "why": self.why,
            "confidence": self.confidence,
            "valid": self.valid,
            "warnings": list(self.warnings),
            "parse_error": self.parse_error,
        }

    def summary(self) -> str:
        parts = [
            f"page={self.observation.page_type.value}",
            f"action={self.action.type.value}",
            f"conf={self.confidence:.2f}",
        ]
        if self.action.selector:
            parts.append(f"sel={self.action.selector!r}")
        if self.previous_action.succeeded is not None:
            parts.append(f"prev_ok={self.previous_action.succeeded}")
        for tag, claim in (("err", self.signals.error), ("otp", self.signals.otp_requested),
                           ("email", self.signals.email_verification_requested),
                           ("captcha", self.signals.captcha_present)):
            if claim.present:
                parts.append(tag + ("+" + claim.detail if claim.detail else ""))
        if not self.valid:
            parts.append(f"INVALID({len(self.warnings)}w)")
        return "Reasoning(" + " ".join(parts) + ")"
