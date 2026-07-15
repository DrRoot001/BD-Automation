"""Deterministic observation of blocking / terminal conditions on a page.

This runs over an already-collected :class:`~..perception.BrowserState` and
reports what is *observed* — validation failure, success, OTP, email
verification, MFA, duplicate account, captcha, navigation failure, unexpected
UI, and the current required fields. It is pure observation (pattern/structure
matching over the captured state), NOT a workflow assumption: it never predicts
"what comes next", only reads what is on screen right now.

It complements the LLM reasoner: both feed the same :class:`DetectedConditions`,
and the agent halts on a blocker flagged by *either* source. Keeping a
deterministic detector means the critical walls (OTP/MFA/duplicate/captcha) are
caught even when the LLM is unavailable or hedges.
"""
from __future__ import annotations

import re
from typing import Optional

from ..perception import BrowserState
from .models import DetectedConditions

# ── evidence pattern tables (lower-cased substrings) ─────────────────────────

_SUCCESS = (
    "thank you for applying", "thanks for applying", "application received",
    "application submitted", "successfully submitted", "application complete",
    "we have received your application", "we've received your application",
    "your application has been received", "your application has been submitted",
)
_EMAIL_VERIFY = (
    "check your email", "verify your email", "confirm your email",
    "verification link", "we sent a verification", "we've sent a verification",
    "we emailed you a link", "we've emailed you a link", "confirm your email address",
    "a verification email", "click the link we sent",
)
_OTP = (
    "one-time code", "one time code", "verification code", "enter the code",
    "enter the 6-digit", "6-digit code", "six-digit code", "security code we sent",
    "enter the code we sent", "code we emailed", "otp",
)
_MFA = (
    "two-factor", "two factor", "2-factor", "2fa", "multi-factor", "mfa",
    "authenticator app", "authentication app", "authenticator code",
    "security key", "push notification", "approve the sign-in", "approve sign in",
    "code from your authenticator",
)
_DUPLICATE = (
    "already exists", "already have an account", "account with this email",
    "already applied", "already submitted an application", "you've already applied",
    "you have already applied", "already have an application on file",
    "duplicate application", "an account already",
)
# USER-FACING captcha / bot-wall prompts (visible text only). The actual widget
# is detected structurally in perception (state.captcha_present); these catch the
# interstitial challenge pages (Cloudflare "verify you are human", etc.).
_CAPTCHA_TEXT = (
    "verify you are human", "are you human", "i'm not a robot", "i am not a robot",
    "security verification", "performing security", "checking your browser",
    "verify you are not a bot", "recaptcha", "hcaptcha", "complete the captcha",
)
_NAV_FAIL = (
    "404", "page not found", "not found", "access denied", "403 forbidden",
    "forbidden", "this site can't be reached", "this page isn't working",
    "no longer available", "the page you are looking for", "job no longer",
    "position is no longer available", "has been removed", "gone",
)

# name/id/placeholder tokens that mark a code-entry input
_CODE_TOKENS = ("otp", "code", "digit", "pin", "passcode", "mfa", "2fa", "token")


def _find(text: str, patterns) -> Optional[str]:
    """Word-boundary substring match against VISIBLE text. Boundaries stop short
    tokens ("2fa", "otp", "mfa", "captcha") from matching inside larger
    alphanumeric runs in scripts/markup/ids — the root cause of the SimplyHired
    "2fa in HTML => MFA_REQUIRED" false positive. `text` must be lower-cased."""
    for p in patterns:
        if re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", text):
            return p
    return None


def _looks_like_code_inputs(state: BrowserState) -> Optional[str]:
    """Structural OTP tell: a cluster of short single-value code inputs, or
    inputs explicitly named otp/code/digit/pin. Returns evidence or None."""
    codeish = 0
    named = 0
    for i in state.inputs:
        if not i.visible or i.field_type not in ("text", "tel", "number", "password"):
            continue
        blob = " ".join([(i.name or ""), (i.element_id or ""), (i.placeholder or ""), (i.label or "")]).lower()
        if any(tok in blob for tok in _CODE_TOKENS):
            named += 1
        # single-char-ish box: no/short label & short current value
        if (len(i.label or "") <= 2) and len((i.value or "")) <= 1 and not i.options:
            codeish += 1
    if named >= 1 and codeish >= 1:
        return f"{named} code-named input(s)"
    if codeish >= 4:
        return f"{codeish} single-character code boxes"
    return None


def detect_conditions(state: BrowserState) -> DetectedConditions:
    """Observe blocking/terminal conditions from the captured state."""
    c = DetectedConditions()
    # Auth-challenge signals are matched against VISIBLE TEXT only — never raw
    # HTML. Substring-matching markup was the root cause of the false-positive
    # halts (a "2fa" token in a bot-wall script => MFA_REQUIRED). User-facing
    # states (MFA/OTP/duplicate/verify) always show their evidence in the text a
    # human would read.
    text = (state.visible_text or "").lower()

    # required fields (visible + required) — the "detect required fields" ask
    c.required_fields = [
        (i.label or i.selector)
        for i in state.inputs
        if i.required and i.visible
    ]

    # validation failure — structured errors are the strongest signal
    if state.validation_errors:
        c.validation_failed = True
        c.validation_evidence = state.validation_errors[0].text[:160]
    elif state.errors:
        c.validation_failed = True
        c.validation_evidence = state.errors[0].text[:160]

    # success — prefer structured success messages
    if state.success_messages:
        c.completed_successfully = True
        c.success_evidence = state.success_messages[0].text[:160]
    else:
        hit = _find(text, _SUCCESS)
        if hit:
            c.completed_successfully = True
            c.success_evidence = f"page text: {hit!r}"

    # duplicate account / already applied
    hit = _find(text, _DUPLICATE)
    if hit:
        c.duplicate_account = True
        c.duplicate_evidence = f"text: {hit!r}"

    # MFA (checked before OTP so 2FA-specific wording wins the label)
    hit = _find(text, _MFA)
    if hit:
        c.mfa_requested = True
        c.mfa_evidence = f"text: {hit!r}"

    # OTP / emailed one-time code
    hit = _find(text, _OTP)
    struct = _looks_like_code_inputs(state)
    if hit or struct:
        c.otp_requested = True
        c.otp_evidence = f"text: {hit!r}" if hit else f"structure: {struct}"

    # email verification
    hit = _find(text, _EMAIL_VERIFY)
    if hit:
        c.email_verification_requested = True
        c.email_verification_evidence = f"text: {hit!r}"

    # captcha — a VISIBLE widget (structured perception) is authoritative; the
    # visible-text prompts catch interstitial challenge pages (Cloudflare, etc.).
    if getattr(state, "captcha_present", False):
        c.captcha_present = True
        c.captcha_evidence = f"visible {getattr(state, 'captcha_kind', '') or 'captcha'} widget"
    else:
        hit = _find(text, _CAPTCHA_TEXT)
        if hit:
            c.captcha_present = True
            c.captcha_evidence = f"text: {hit!r}"

    # navigation failure — a strong error marker AND no usable application surface
    hit = _find(text, _NAV_FAIL) or ("404" in (state.title or "").lower() and "404")
    if hit and not state.has_form:
        c.navigation_failed = True
        c.navigation_evidence = f"error page: {hit!r}, no form present"

    # unexpected UI — a visible modal that is NOT the application form
    for m in state.modals:
        if not m.looks_like_application:
            c.unexpected_ui = True
            c.unexpected_ui_evidence = f"overlay/modal: {m.text_preview[:80]!r}"
            break

    return c


def merge_reasoning_signals(conditions: DetectedConditions, reasoning) -> None:
    """Fold an LLM :class:`ReasoningOutput`'s signal claims into the observed
    conditions (belt-and-suspenders: the agent halts on a blocker flagged by
    either the detector or the reasoner). Best-effort; tolerant of shape."""
    try:
        s = reasoning.signals
    except AttributeError:
        return

    def _apply(present_attr: str, claim, evidence_attr: str, detail: str) -> None:
        try:
            if claim.present:
                setattr(conditions, present_attr, True)
                if not getattr(conditions, evidence_attr):
                    setattr(conditions, evidence_attr, f"reasoner: {claim.evidence or detail}"[:160])
        except AttributeError:
            pass

    _apply("otp_requested", s.otp_requested, "otp_evidence", "otp")
    _apply("email_verification_requested", s.email_verification_requested,
           "email_verification_evidence", "email verification")
    _apply("mfa_requested", s.mfa_requested, "mfa_evidence", "mfa")
    _apply("duplicate_account", s.duplicate_account, "duplicate_evidence", "duplicate")
    _apply("validation_failed", s.error, "validation_evidence", "error")
    # NOTE: the reasoner's captcha claim is deliberately NOT merged. A captcha
    # HALT must be grounded in a VISIBLE widget (perception's state.captcha_present,
    # already set in detect_conditions). Greenhouse/Lever ship a HIDDEN
    # #g-recaptcha-response textarea (invisible reCAPTCHA) on EVERY form; letting
    # the LLM flag that from the DOM halted the agent before it filled a single
    # field. Vision-observed, not DOM-assumed.

    # success + wrong-page come from the observation block, not signals
    try:
        from ..reasoning import PageType
        if reasoning.observation.page_type == PageType.SUCCESS:
            conditions.completed_successfully = True
            if not conditions.success_evidence:
                conditions.success_evidence = "reasoner: page classified SUCCESS"
    except Exception:
        pass
