"""Regression tests for evidence-based condition detection (no false positives).

Pins the SimplyHired failure: a Cloudflare bot-wall whose raw HTML contained a
'2fa' token made the agent halt as MFA_REQUIRED at step 1 (0 LLM calls) — an
assumption from markup, not observed evidence. detect_conditions must now:
  * match auth-challenge keywords against VISIBLE TEXT only, with word boundaries;
  * treat captcha from a VISIBLE widget (perception) or a user-facing prompt.

Run: `pytest backend/app/browser_automation/tests/test_conditions_detection.py -q`
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.perception import BrowserState  # noqa: E402
from backend.app.browser_automation.autonomous.conditions import detect_conditions  # noqa: E402


def _state(visible_text="", html="", captcha_present=False, captcha_kind=""):
    st = BrowserState(visible_text=visible_text, html=html)
    st.captcha_present = captcha_present
    st.captcha_kind = captcha_kind
    return st


# ── the exact false positive that broke the SimplyHired run ───────────────────

def test_markup_tokens_do_not_trigger_auth_halts():
    # Raw HTML/scripts contain '2fa', 'mfa', 'otp', 'captcha' tokens (as they do
    # on countless real pages) but the VISIBLE text is a normal job listing.
    html = ("<script>var a2fabc='x';window.__mfaToken='q';dataLayer.push({otp:1});"
            "loadCaptchaScript();</script><div class='r2fa-widget'></div>")
    st = _state(visible_text="Senior Software Engineer at Acme. Apply now. Full-time, remote.",
                html=html)
    c = detect_conditions(st)
    assert c.mfa_requested is False
    assert c.otp_requested is False
    assert c.captcha_present is False
    assert c.duplicate_account is False


def test_word_boundary_blocks_embedded_tokens():
    # "2fabulous" / "captcha-less" must NOT match 2fa / captcha.
    st = _state(visible_text="Our 2fabulous captcha-less onboarding is otpional and mfaster.")
    c = detect_conditions(st)
    assert c.mfa_requested is False
    assert c.captcha_present is False
    assert c.otp_requested is False


# ── real, VISIBLE challenges must still be detected ───────────────────────────

def test_visible_captcha_widget_detected():
    # Cloudflare interstitial: perception flags a visible widget.
    st = _state(visible_text="www.simplyhired.com\nPerforming security verification\n"
                             "Verify you are human",
                captcha_present=True, captcha_kind="turnstile")
    c = detect_conditions(st)
    assert c.captcha_present is True
    assert "turnstile" in c.captcha_evidence


def test_cloudflare_prompt_text_detected_without_widget():
    st = _state(visible_text="Checking your browser before accessing the site. "
                             "Verify you are human.")
    c = detect_conditions(st)
    assert c.captcha_present is True


def test_real_mfa_text_detected():
    st = _state(visible_text="Two-factor authentication required. "
                             "Open your authenticator app and enter the code.")
    c = detect_conditions(st)
    assert c.mfa_requested is True


def test_real_duplicate_text_detected():
    st = _state(visible_text="An account with this email already exists. Please sign in.")
    c = detect_conditions(st)
    assert c.duplicate_account is True


def test_2fa_standalone_token_in_visible_text_detected():
    st = _state(visible_text="Enter your 2FA code to continue.")
    c = detect_conditions(st)
    assert c.mfa_requested is True


# ── the reasoner's captcha claim must NOT force a halt (Greenhouse bug) ────────

def test_reasoner_captcha_claim_does_not_set_halting_condition():
    # Greenhouse ships a HIDDEN g-recaptcha-response textarea on every form; the
    # LLM used to flag captcha from it and the agent halted at step 1. A captcha
    # HALT must require a VISIBLE widget (perception), not the reasoner's DOM read.
    from backend.app.browser_automation.autonomous.conditions import merge_reasoning_signals
    from backend.app.browser_automation.reasoning.models import ReasoningOutput

    reasoning = ReasoningOutput.from_raw({
        "observation": {"page_type": "FORM"},
        "signals": {"captcha_present": {"present": True,
                                        "evidence": "hidden g-recaptcha-response textarea present"}},
        "decision": {"action": {"type": "FILL", "selector": "#fn", "value": "x", "field_label": "First"},
                     "why": "fill"},
    })
    from backend.app.browser_automation.autonomous.models import DetectedConditions
    cond = DetectedConditions()  # perception saw NO visible captcha widget
    merge_reasoning_signals(cond, reasoning)
    assert cond.captcha_present is False  # reasoner claim did not force a halt
    # other reasoner signals still merge (they read visible text, not hidden DOM)
    reasoning2 = ReasoningOutput.from_raw({
        "observation": {"page_type": "VERIFICATION"},
        "signals": {"otp_requested": {"present": True, "evidence": "6 code boxes visible"}},
        "decision": {"action": {"type": "HANDLE_VERIFICATION"}, "why": "otp"},
    })
    cond2 = DetectedConditions()
    merge_reasoning_signals(cond2, reasoning2)
    assert cond2.otp_requested is True


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
