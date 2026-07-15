"""Tests for the resilience / recovery layer (RecoveryController + agent wiring).

Proves the agent RECOVERS instead of failing immediately across: blocking
modals/popups, slow-loading pages, new-tab popups, validation errors,
conditional forms, and failed/timed-out actions (retry). E2E cases drive the
real AutonomousAgent over file:// fixtures with a scripted reasoner (no LLM);
unit cases exercise RecoveryController directly.

Run: `pytest backend/app/browser_automation/tests/test_recovery.py -q`
"""
import os
import re
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from playwright.async_api import async_playwright  # noqa: E402

from backend.app.browser_automation.autonomous import (  # noqa: E402
    ActionResult,
    AgentStatus,
    AutonomousAgent,
    RecoveryController,
)
from backend.app.browser_automation.perception import (  # noqa: E402
    BrowserState,
    BrowserStateCollector,
    ScreenshotConfig,
    StabilityConfig,
)
from backend.app.browser_automation.reasoning import DecisionEngine  # noqa: E402
from backend.app.browser_automation.reasoning.models import (  # noqa: E402
    ActionType,
    NextAction,
    ReasoningOutput,
)


# ── scripted reasoner ─────────────────────────────────────────────────────────

class _Dummy:
    pass


_GROUNDER = DecisionEngine(llm_client=_Dummy())
_TEXTY = ("text", "email", "tel", "url", "number", "textarea")


def _raw(atype, **params):
    action = {"type": atype}
    action.update(params)
    return {
        "observation": {"page_type": "FORM", "page_evidence": "x", "what_changed": "x", "change_evidence": ""},
        "previous_action_assessment": {"had_previous_action": False, "succeeded": None, "evidence": ""},
        "signals": {},
        "decision": {"action": action, "why": "grounded", "confidence": 0.9},
    }


class ScriptedEngine:
    """Fill required fields → submit; else click an entry button (open/apply)."""
    async def decide(self, state, objective, *, browser_memory=None, conversation=None,
                     step=None, max_steps=None, screenshot=None):
        out = ReasoningOutput.from_raw(self._plan(state))
        _GROUNDER._add_grounding_warnings(out, state)
        _GROUNDER._record_turn(out, state, conversation, step)
        return out

    def _plan(self, state):
        for i in state.inputs:
            if not i.visible or not i.required or i.is_filled or i.disabled:
                continue
            if i.field_type == "select" and i.options:
                opt = next((o for o in i.options if o and "select" not in o.lower()), i.options[-1])
                return _raw("SELECT_OPTION", selector=i.selector, value=opt)
            if i.field_type in _TEXTY:
                val = "jane@example.com" if i.field_type == "email" else "Jane"
                return _raw("FILL", selector=i.selector, value=val, field_label=i.label)
        subs = [b for b in state.buttons if b.is_submit and b.visible and not b.disabled]
        if subs:
            return _raw("SUBMIT", selector=subs[0].selector)
        # no form yet → click an entry/open button to reveal it
        for b in state.buttons:
            if b.visible and not b.disabled and re.search(r"open|apply|start|begin|get started", b.text, re.I):
                return _raw("CLICK", selector=b.selector, value=b.text)
        return _raw("OBSERVE")


class ValidationEngine:
    """Submit first (triggers server validation), then fill on the error, resubmit."""
    async def decide(self, state, objective, *, browser_memory=None, conversation=None,
                     step=None, max_steps=None, screenshot=None):
        out = ReasoningOutput.from_raw(self._plan(state))
        _GROUNDER._add_grounding_warnings(out, state)
        _GROUNDER._record_turn(out, state, conversation, step)
        return out

    def _plan(self, state):
        empty = [i for i in state.inputs if i.field_type in _TEXTY and not i.is_filled and i.visible]
        subs = [b for b in state.buttons if b.is_submit and b.visible and not b.disabled]
        # React to ANY surfaced error (validation errors on a bare banner are
        # classified kind='error' when not inside a field container).
        has_error = bool(state.errors)
        if empty and has_error:
            i = empty[0]
            return _raw("FILL", selector=i.selector,
                        value="jane@example.com" if i.field_type == "email" else "Jane",
                        field_label=i.label)
        if subs and not empty:
            return _raw("SUBMIT", selector=subs[0].selector)
        if subs and empty and not has_error:
            return _raw("SUBMIT", selector=subs[0].selector)  # provoke validation
        return _raw("OBSERVE")


def _agent(engine=None, **kw):
    return AutonomousAgent(
        engine=engine or ScriptedEngine(),
        collector=BrowserStateCollector(
            screenshot=ScreenshotConfig(enabled=False),
            stability=StabilityConfig(quiet_ms=120, timeout_ms=1_200, wait_network_idle=False),
        ),
        max_steps=kw.pop("max_steps", 20),
        **kw,
    )


async def _run(html, engine=None, files=None, **kw):
    """Write fixture(s), run the agent over the primary one. `files` is a dict of
    extra {name: html} written alongside so window.open('name') resolves."""
    d = tempfile.mkdtemp()
    for name, content in (files or {}).items():
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(content)
    main = os.path.join(d, "main.html")
    with open(main, "w", encoding="utf-8") as f:
        f.write(html)
    url = "file:///" + main.replace(os.sep, "/").lstrip("/")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url)
            return await _agent(engine, **kw).run(page, "Complete the application")
        finally:
            await browser.close()


# ── fixtures ──────────────────────────────────────────────────────────────────

_MODAL_BLOCKS = """<!doctype html><html><head><title>Apply</title></head><body>
<div role="dialog" aria-modal="true" class="modal" id="nl"
     style="position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999">
  <div style="background:#fff;padding:20px;margin:40px auto;max-width:300px">
    <h3>Subscribe to job alerts</h3><p>Get roles in your inbox.</p>
    <button aria-label="Close" id="x">×</button>
  </div>
</div>
<form>
  <label for="fn">First name *<input id="fn" name="first_name" required></label>
  <button type="button" id="go">Submit Application</button>
</form>
<script>
document.getElementById('x').addEventListener('click', () => document.getElementById('nl').remove());
document.getElementById('go').addEventListener('click', () => {
  if (document.getElementById('fn').value.trim())
    document.body.innerHTML='<h1>Thank you for applying! Application received.</h1>';
});
</script></body></html>"""

_SLOW_LOAD = """<!doctype html><html><head><title>Loading</title></head><body>
<div id="root"></div>
<script>
setTimeout(() => {
  document.getElementById('root').innerHTML =
    '<form><label>First name *<input id="fn" required></label>'
    + '<button type="button" id="go">Submit Application</button></form>';
  document.getElementById('go').addEventListener('click', () => {
    if (document.getElementById('fn').value.trim())
      document.body.innerHTML='<h1>Thank you for applying! Application received.</h1>';
  });
}, 900);
</script></body></html>"""

_POPUP_FORM = """<!doctype html><html><head><title>Application</title></head><body>
<form>
  <label>First name *<input id="fn" required></label>
  <button type="button" id="go">Submit Application</button>
</form>
<script>
document.getElementById('go').addEventListener('click', () => {
  if (document.getElementById('fn').value.trim())
    document.body.innerHTML='<h1>Thank you for applying! Application received.</h1>';
});
</script></body></html>"""

_POPUP_MAIN = """<!doctype html><html><head><title>Job</title></head><body>
<h1>Great role</h1>
<button id="open">Open application</button>
<script>
document.getElementById('open').addEventListener('click', () => window.open('form.html', '_blank'));
</script></body></html>"""

_VALIDATION = """<!doctype html><html><head><title>Apply</title></head><body>
<form>
  <label for="em">Email<input id="em" name="email" type="email"></label>
  <div id="err"></div>
  <button type="button" id="go">Submit Application</button>
</form>
<script>
const em = document.getElementById('em'), err = document.getElementById('err');
em.addEventListener('input', () => { err.innerHTML=''; });  // clearing recovers
document.getElementById('go').addEventListener('click', () => {
  err.innerHTML='';
  const v = em.value.trim();
  if (!v || v.indexOf('@') === -1) {
    err.innerHTML = '<div class="error-message">A valid email is required.</div>';
  } else {
    document.body.innerHTML='<h1>Thank you for applying! Application received.</h1>';
  }
});
</script></body></html>"""

_CONDITIONAL = """<!doctype html><html><head><title>Apply</title></head><body>
<form>
  <label for="fn">First name *<input id="fn" name="first_name" required></label>
  <div id="extra"></div>
  <button type="button" id="go">Submit Application</button>
</form>
<script>
const fn = document.getElementById('fn');
fn.addEventListener('input', () => {
  // Filling first name reveals a conditionally-required email field.
  if (fn.value.trim() && !document.getElementById('em')) {
    document.getElementById('extra').innerHTML =
      '<label>Email *<input id="em" name="email" type="email" required></label>';
  }
});
document.getElementById('go').addEventListener('click', () => {
  const em = document.getElementById('em');
  if (fn.value.trim() && em && em.value.trim())
    document.body.innerHTML='<h1>Thank you for applying! Application received.</h1>';
});
</script></body></html>"""


def _recovers(res, needle):
    return any(h["action"] == "RECOVER" and needle in (h["note"] or "") for h in res.history)


# ── E2E recovery scenarios ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recovers_from_blocking_modal():
    res = await _run(_MODAL_BLOCKS)
    assert res.status == AgentStatus.SUBMITTED, res.summary()
    assert _recovers(res, "dismissed"), [h for h in res.history if h["action"] == "RECOVER"]


@pytest.mark.asyncio
async def test_recovers_from_slow_load():
    res = await _run(_SLOW_LOAD)
    assert res.status == AgentStatus.SUBMITTED, res.summary()
    assert _recovers(res, "slow load")


@pytest.mark.asyncio
async def test_adopts_popup_new_tab():
    res = await _run(_POPUP_MAIN, files={"form.html": _POPUP_FORM})
    assert res.status == AgentStatus.SUBMITTED, res.summary()
    assert _recovers(res, "new tab")


@pytest.mark.asyncio
async def test_recovers_from_validation_error():
    res = await _run(_VALIDATION, engine=ValidationEngine())
    assert res.status == AgentStatus.SUBMITTED, res.summary()
    # a validation error was observed and then recovered from (not fatal)
    assert res.conditions.validation_failed is True


@pytest.mark.asyncio
async def test_handles_conditional_form():
    res = await _run(_CONDITIONAL)
    assert res.status == AgentStatus.SUBMITTED, res.summary()
    # the conditionally-revealed email field was detected as a required field
    assert res.conditions.required_fields


# ── unit: RecoveryController mechanics ────────────────────────────────────────

class _FakeLoc:
    async def count(self):
        return 0

    @property
    def first(self):
        return self

    async def is_visible(self):
        return False

    async def scroll_into_view_if_needed(self, timeout=None):
        return None

    async def click(self, timeout=None):
        return None


class _FakePage:
    def locator(self, sel):
        return _FakeLoc()

    async def evaluate(self, *a, **k):
        return None


class _SeqExecutor:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def execute(self, page, frame, action, context):
        r = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return r


@pytest.mark.asyncio
async def test_recover_action_retries_transient_failure():
    rc = RecoveryController()
    action = NextAction(type=ActionType.CLICK, selector="#x")
    ex = _SeqExecutor([ActionResult(ok=True, note="ok on retry")])
    res = await rc.recover_action(_FakePage(), None, BrowserState(), action, ex, {})
    assert res is not None and res.ok is True
    assert ex.calls == 1  # one retry happened


@pytest.mark.asyncio
async def test_recover_action_respects_retry_cap():
    rc = RecoveryController(max_action_retries=2)
    action = NextAction(type=ActionType.CLICK, selector="#x")
    ex = _SeqExecutor([ActionResult(ok=False, note="still failing")])
    assert (await rc.recover_action(_FakePage(), None, BrowserState(), action, ex, {})) is not None
    assert (await rc.recover_action(_FakePage(), None, BrowserState(), action, ex, {})) is not None
    # third attempt exceeds the cap → no further retry
    assert (await rc.recover_action(_FakePage(), None, BrowserState(), action, ex, {})) is None


@pytest.mark.asyncio
async def test_recover_action_skips_non_retryable():
    rc = RecoveryController()
    action = NextAction(type=ActionType.OBSERVE)
    ex = _SeqExecutor([ActionResult(ok=True)])
    assert (await rc.recover_action(_FakePage(), None, BrowserState(), action, ex, {})) is None
    assert ex.calls == 0


def test_page_looks_unready():
    rc = RecoveryController()
    blank = BrowserState(is_stable=True, visible_text="")
    assert rc.page_looks_unready(blank) is True
    from backend.app.browser_automation.perception import InputField
    ready = BrowserState(is_stable=True, visible_text="lots of content here " * 3)
    ready.inputs = [InputField(selector="#fn", field_type="text", label="First name")]
    assert rc.page_looks_unready(ready) is False


def test_wait_and_dismiss_budgets_are_bounded():
    rc = RecoveryController(loading_wait_budget=1, dismiss_budget=1)
    assert rc.can_wait_for_load() is True
    rc._loading_budget = 0
    assert rc.can_wait_for_load() is False
    assert rc.can_dismiss() is True
    rc._dismiss_budget = 0
    assert rc.can_dismiss() is False


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
