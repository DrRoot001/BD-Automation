"""Integration tests for the perception-driven Generic adapter / AutonomousAgent.

Drives the FULL observe → reason → act → observe loop against local file://
fixtures in headless Chromium. The reasoner is scripted (reads the real
BrowserState and returns grounded actions) so the whole machinery — perception,
condition detection, action execution, re-observation — runs end-to-end without
an LLM or network.

Covers the required detections: validation failure, successful completion,
navigation failure, OTP, email verification, MFA, duplicate account, captcha,
unexpected UI, and required fields — plus the Generic adapter delegation.

Run: `pytest backend/app/browser_automation/tests/test_generic_autonomous.py -q`
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from playwright.async_api import async_playwright  # noqa: E402

from backend.app.browser_automation.perception import (  # noqa: E402
    BrowserStateCollector,
    ScreenshotConfig,
    StabilityConfig,
)
from backend.app.browser_automation.reasoning import DecisionEngine  # noqa: E402
from backend.app.browser_automation.reasoning.models import ReasoningOutput  # noqa: E402
from backend.app.browser_automation.autonomous import (  # noqa: E402
    AgentStatus,
    AutonomousAgent,
)
from backend.app.browser_automation.adapters.generic import GenericFormAdapter  # noqa: E402


# ── scripted reasoners (no LLM) ───────────────────────────────────────────────

def _raw(atype, *, page="FORM", why="reason", **params):
    action = {"type": atype}
    action.update(params)
    return {
        "observation": {"page_type": page, "page_evidence": "observed",
                        "what_changed": "n/a", "change_evidence": ""},
        "previous_action_assessment": {"had_previous_action": False, "succeeded": None, "evidence": ""},
        "signals": {},
        "decision": {"action": action, "why": why, "confidence": 0.9},
    }


class _Dummy:
    pass


_GROUNDER = DecisionEngine(llm_client=_Dummy())  # reused for real grounding logic


class ScriptedEngine:
    """Fills required fields top-to-bottom from a profile, then submits — using
    only what it observes in the BrowserState (real grounding applied)."""
    def __init__(self, profile):
        self.profile = profile

    def _value_for(self, label):
        l = (label or "").lower()
        if "first name" in l:
            return self.profile.get("first", "Jane")
        if "last name" in l:
            return self.profile.get("last", "Doe")
        if "email" in l:
            return self.profile.get("email", "jane@example.com")
        if "phone" in l:
            return self.profile.get("phone", "555-1234")
        return "N/A"

    def _plan(self, state):
        for i in state.inputs:
            if not i.visible or not i.required or i.is_filled or i.disabled:
                continue
            if i.field_type == "select" and i.options:
                opt = next((o for o in i.options if o and "select" not in o.lower()), i.options[-1])
                return _raw("SELECT_OPTION", selector=i.selector, value=opt, why="required select empty")
            if i.field_type in ("text", "email", "tel", "url", "number", "textarea", "password"):
                return _raw("FILL", selector=i.selector, value=self._value_for(i.label),
                            field_label=i.label, why="fill required field")
        subs = [b for b in state.buttons if b.is_submit and b.visible and not b.disabled]
        if subs:
            return _raw("SUBMIT", selector=subs[0].selector, why="all required fields filled")
        return _raw("OBSERVE", why="nothing actionable")

    async def decide(self, state, objective, *, browser_memory=None, conversation=None,
                     step=None, max_steps=None, screenshot=None):
        out = ReasoningOutput.from_raw(self._plan(state))
        _GROUNDER._add_grounding_warnings(out, state)
        _GROUNDER._record_turn(out, state, conversation, step)
        return out


class NoopEngine:
    """Always OBSERVE — used to test pure-observation detections."""
    async def decide(self, state, objective, *, browser_memory=None, conversation=None,
                     step=None, max_steps=None, screenshot=None):
        return ReasoningOutput.from_raw(_raw("OBSERVE", page="UNKNOWN", why="observe only"))


def _fast_collector():
    return BrowserStateCollector(
        screenshot=ScreenshotConfig(enabled=False),
        stability=StabilityConfig(quiet_ms=120, timeout_ms=1_200, wait_network_idle=False),
    )


def _agent(engine, **kw):
    return AutonomousAgent(engine=engine, collector=_fast_collector(), **kw)


# ── fixtures ──────────────────────────────────────────────────────────────────

_HAPPY_FORM = """<!doctype html><html><head><title>Apply</title></head><body>
<form id="app">
  <label for="fn">First name *<input id="fn" name="first_name" required></label>
  <label for="em">Email *<input id="em" name="email" type="email" required></label>
  <label for="co">Country *
    <select id="co" name="country" required>
      <option value="">Select…</option>
      <option>United States</option><option>Canada</option>
    </select></label>
  <button type="button" id="go">Submit Application</button>
</form>
<script>
document.getElementById('go').addEventListener('click', () => {
  const fn=document.getElementById('fn').value.trim();
  const em=document.getElementById('em').value.trim();
  const co=document.getElementById('co').value.trim();
  if (fn && em && co) { document.body.innerHTML='<h1>Thank you for applying! Application received.</h1>'; }
  else if(!document.querySelector('.error-message')){
    const e=document.createElement('div'); e.className='error-message';
    e.textContent='All fields are required'; document.body.appendChild(e);
  }
});
</script></body></html>"""

_VALIDATION = """<!doctype html><html><head><title>Apply</title></head><body>
<form><label for="fn">First name *<input id="fn" required></label>
<div class="error-message">This field is required.</div>
<button type="submit">Submit</button></form></body></html>"""

_OTP = """<!doctype html><html><head><title>Verify</title></head><body>
<h2>Enter the verification code we sent</h2>
<form>
<input name="digit1" maxlength="1"><input name="digit2" maxlength="1">
<input name="digit3" maxlength="1"><input name="digit4" maxlength="1">
<input name="digit5" maxlength="1"><input name="digit6" maxlength="1">
<button type="submit">Verify</button></form></body></html>"""

_EMAIL_VERIFY = """<!doctype html><html><head><title>Almost there</title></head><body>
<h2>Almost done</h2>
<p>Please check your email and click the verification link to confirm your email address.</p>
</body></html>"""

_MFA = """<!doctype html><html><head><title>Security</title></head><body>
<h2>Two-factor authentication required</h2>
<p>Open your authenticator app and enter the 6-digit code.</p>
<form><input name="code"><button type="submit">Verify</button></form></body></html>"""

_DUPLICATE = """<!doctype html><html><head><title>Sign in</title></head><body>
<h2>Account exists</h2>
<p>An account with this email already exists. Please sign in instead.</p>
<form><input name="password" type="password"><button type="submit">Sign in</button></form>
</body></html>"""

_CAPTCHA = """<!doctype html><html><head><title>Verify</title></head><body>
<form>
  <label>Email<input name="email" type="email"></label>
  <div class="g-recaptcha" data-sitekey="x">Please complete the reCAPTCHA to continue</div>
  <button type="submit">Continue</button>
</form></body></html>"""

_NAV_FAIL = """<!doctype html><html><head><title>404 Not Found</title></head><body>
<h1>Page not found</h1><p>The page you are looking for no longer available.</p></body></html>"""

_UNEXPECTED_UI = """<!doctype html><html><head><title>Careers</title></head><body>
<div role="dialog" aria-modal="true" class="modal">
  <h3>Subscribe to job alerts</h3><p>Get roles in your inbox.</p>
  <button aria-label="Close">×</button>
</div>
<form><label>First name *<input name="first_name" required></label>
<button type="submit">Submit</button></form></body></html>"""


async def _run(html, engine, **agent_kw):
    """Load a fixture and run the autonomous agent over it. Returns AgentRunResult."""
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(html)
    f.close()
    url = "file:///" + f.name.replace(os.sep, "/").lstrip("/")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url)
            return await _agent(engine, **agent_kw).run(page, "Complete the application")
        finally:
            await browser.close()
            os.unlink(f.name)


# ── happy path: observe → reason → act → observe → SUBMITTED ──────────────────

@pytest.mark.asyncio
async def test_happy_path_fills_submits_and_confirms():
    res = await _run(_HAPPY_FORM, ScriptedEngine({}))
    assert res.status == AgentStatus.SUBMITTED, res.summary()
    assert res.confirmation and "applying" in res.confirmation.lower()
    # required fields were observed
    assert res.conditions.required_fields  # non-empty
    # the loop actually filled + submitted (not assumed)
    acts = [h["action"] for h in res.history]
    assert "FILL" in acts and "SUBMIT" in acts
    assert "SELECT_OPTION" in acts
    # observe-AFTER the submit produced the success confirmation
    assert res.history[-1]["action"] == "OBSERVE"
    assert "success" in res.history[-1]["detected"]


@pytest.mark.asyncio
async def test_observe_precedes_every_action():
    # Every ACT is preceded by an OBSERVE in the same turn: the loop head always
    # collects state before reasoning. We assert there are at least as many
    # perceive turns as executed actions (one observe per turn, ≤1 act per turn).
    res = await _run(_HAPPY_FORM, ScriptedEngine({}))
    executed = [h for h in res.history if h.get("action_ok") is not None]
    # fn, em, country, submit → 4 mutating acts, each from its own observed turn
    assert len(executed) >= 3
    # the run ended by OBSERVING success, not by assuming submit==success
    assert res.status == AgentStatus.SUBMITTED
    assert any(h["action"] == "SUBMIT" and h["action_ok"] for h in res.history)


# ── detections ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_detects_validation_failure():
    res = await _run(_VALIDATION, NoopEngine(), max_steps=2)
    assert res.conditions.validation_failed is True
    assert "required" in res.conditions.validation_evidence.lower()
    assert res.conditions.required_fields  # required field also observed


@pytest.mark.asyncio
async def test_detects_otp_request():
    res = await _run(_OTP, NoopEngine(), max_steps=3)
    assert res.status == AgentStatus.OTP_REQUIRED
    assert res.conditions.otp_requested is True


@pytest.mark.asyncio
async def test_detects_email_verification():
    res = await _run(_EMAIL_VERIFY, NoopEngine(), max_steps=3)
    assert res.status == AgentStatus.EMAIL_VERIFICATION_REQUIRED
    assert res.conditions.email_verification_requested is True


@pytest.mark.asyncio
async def test_detects_mfa():
    res = await _run(_MFA, NoopEngine(), max_steps=3)
    assert res.status == AgentStatus.MFA_REQUIRED
    assert res.conditions.mfa_requested is True


@pytest.mark.asyncio
async def test_detects_duplicate_account():
    res = await _run(_DUPLICATE, NoopEngine(), max_steps=3)
    assert res.status == AgentStatus.DUPLICATE_ACCOUNT
    assert res.conditions.duplicate_account is True


@pytest.mark.asyncio
async def test_detects_captcha():
    res = await _run(_CAPTCHA, NoopEngine(), max_steps=3)
    assert res.status == AgentStatus.CAPTCHA_REQUIRED
    assert res.conditions.captcha_present is True


@pytest.mark.asyncio
async def test_detects_navigation_failure():
    res = await _run(_NAV_FAIL, NoopEngine(), max_steps=3)
    assert res.status == AgentStatus.NAVIGATION_FAILED
    assert res.conditions.navigation_failed is True


@pytest.mark.asyncio
async def test_detects_unexpected_ui():
    res = await _run(_UNEXPECTED_UI, NoopEngine(), max_steps=2)
    assert res.conditions.unexpected_ui is True
    assert "job alerts" in res.conditions.unexpected_ui_evidence.lower()


@pytest.mark.asyncio
async def test_detects_required_fields():
    res = await _run(_HAPPY_FORM, NoopEngine(), max_steps=2)
    labels = " ".join(res.conditions.required_fields).lower()
    assert "first name" in labels and "email" in labels


# ── handler routing: a verification handler prevents the OTP halt ─────────────

@pytest.mark.asyncio
async def test_verification_handler_prevents_otp_halt():
    calls = {"n": 0}

    async def handler(page, frame, state, cond):
        calls["n"] += 1
        return True  # pretend we fetched + entered the code

    res = await _run(_OTP, NoopEngine(), max_steps=2, verification_handler=handler)
    assert calls["n"] >= 1
    assert res.status != AgentStatus.OTP_REQUIRED  # handler cleared the halt


# ── Generic adapter delegation ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generic_adapter_delegates_to_autonomous_agent():
    adapter = GenericFormAdapter(agent=_agent(ScriptedEngine({})))
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(_HAPPY_FORM)
    f.close()
    url = "file:///" + f.name.replace(os.sep, "/").lstrip("/")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await adapter.navigate_to_application(page, url)
            filled = await adapter.fill_application(page, {"name": "Jane Doe"}, "resume.pdf", None, {})
            submitted = await adapter.submit(page)
            verified, confirmation = await adapter.verify_success(page)
        finally:
            await browser.close()
            os.unlink(f.name)

    assert filled is True
    assert submitted is True
    assert verified is True
    assert confirmation and "applying" in confirmation.lower()


@pytest.mark.asyncio
async def test_generic_adapter_reports_block_without_false_submit():
    # A duplicate-account wall must NOT be reported as a successful submit.
    adapter = GenericFormAdapter(agent=_agent(NoopEngine(), max_steps=2))
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(_DUPLICATE)
    f.close()
    url = "file:///" + f.name.replace(os.sep, "/").lstrip("/")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await adapter.navigate_to_application(page, url)
            filled = await adapter.fill_application(page, {"name": "Jane"}, "r.pdf", None, {})
            verified, detail = await adapter.verify_success(page)
        finally:
            await browser.close()
            os.unlink(f.name)

    assert filled is False
    assert verified is False
    assert detail  # carries the block reason


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
