"""Unit tests for the evidence-grounded reasoning / decision engine.

No browser and no network: BrowserState objects are built directly and a fake
LLM returns canned structured JSON, so we test parsing, the perception prompt,
grounding (hallucination + submit/done guards), memory bookkeeping, and the
fallback path deterministically.

Run: `pytest backend/app/browser_automation/tests/test_reasoning_engine.py -q`
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.llm import LLMUnavailable  # noqa: E402
from backend.app.browser_automation.perception import (  # noqa: E402
    BrowserState,
    ButtonElement,
    InputField,
    NavigationState,
    PageMessage,
)
from backend.app.browser_automation.reasoning import (  # noqa: E402
    ActionType,
    BrowserMemory,
    ConversationMemory,
    DecisionEngine,
    PageType,
    ReasoningOutput,
    build_user_turn,
    render_browser_state,
)


# ── Fakes / fixtures ──────────────────────────────────────────────────────────

class FakeLLM:
    """Records calls and returns a canned dict (or raises a canned exception)."""
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def generate_json(self, prompt, image_bytes=None, temperature=0.1,
                            timeout_s=25.0, system=None):
        self.calls.append({"prompt": prompt, "image_bytes": image_bytes, "system": system})
        if isinstance(self.response, Exception):
            raise self.response
        if callable(self.response):
            return self.response(len(self.calls))
        return self.response


def _form_state(*, fn_value="", email_value="a@b.com", errors=None, success=None,
                captcha_text="") -> BrowserState:
    st = BrowserState(url="https://jobs.example.com/apply", title="Apply — Engineer")
    st.navigation = NavigationState(url=st.url, title=st.title, ready_state="complete")
    st.is_stable = True
    st.stability_reason = "quiescent"
    st.screenshot = b"\x89PNG\r\n\x1a\nFAKEBYTES"
    st.visible_text = "First name Email Submit Application " + captcha_text
    st.html = "<form>...</form>" + captcha_text
    st.inputs = [
        InputField(selector="#fn", field_type="text", label="First name", required=True, value=fn_value),
        InputField(selector="#em", field_type="email", label="Email", required=True, value=email_value),
    ]
    st.buttons = [
        ButtonElement(selector="#submit_app", text="Submit Application", button_type="submit", is_submit=True),
    ]
    if errors:
        st.messages += [PageMessage(kind="validation", text=t, associated_field="Last name") for t in errors]
    if success:
        st.messages += [PageMessage(kind="success", text=success)]
    return st


def _resp(action, *, page="FORM", signals=None, prev=None, why="grounded reason", conf=0.9):
    sig = {"error": {"present": False, "evidence": ""},
           "otp_requested": {"present": False, "evidence": ""},
           "email_verification_requested": {"present": False, "evidence": ""},
           "captcha_present": {"present": False, "evidence": ""}}
    if signals:
        for k, v in signals.items():
            sig[k] = v
    return {
        "observation": {"page_type": page, "page_evidence": "inputs visible",
                        "what_changed": "first observation", "change_evidence": ""},
        "previous_action_assessment": prev or {"had_previous_action": False, "succeeded": None, "evidence": "n/a"},
        "signals": sig,
        "decision": {"action": action, "why": why, "confidence": conf},
    }


# ── Prompt / rendering ────────────────────────────────────────────────────────

def test_render_browser_state_contains_evidence():
    st = _form_state(errors=["Last name is required"])
    txt = render_browser_state(st)
    assert "#fn" in txt and "First name" in txt
    assert "#submit_app" in txt and "Submit Application" in txt
    assert "VALIDATION ERRORS" in txt and "Last name is required" in txt
    assert "required" in txt


def test_build_user_turn_includes_objective_memory_and_prev_action():
    st = _form_state()
    conv = ConversationMemory(objective="Apply to the Engineer role")
    from backend.app.browser_automation.reasoning import ConversationTurn
    conv.add(ConversationTurn(step=1, action_type="FILL", action_summary="FILL #fn = 'Jane'",
                              reasoning_summary="first field"))
    bmem = BrowserMemory(host="jobs.example.com", last_outcome="SUBMITTED", login_required=False)
    msg = build_user_turn(st, "Apply to the Engineer role", browser_memory=bmem,
                          conversation=conv, step=2, max_steps=60)
    assert "CURRENT OBJECTIVE" in msg and "Engineer role" in msg
    assert "PREVIOUS ACTION" in msg and "FILL #fn" in msg
    assert "BROWSER MEMORY" in msg and "jobs.example.com" in msg
    assert "STRUCTURED PAGE STATE" in msg and "#fn" in msg


# ── Decision engine: happy path + grounding ──────────────────────────────────

@pytest.mark.asyncio
async def test_fill_decision_is_parsed_and_grounded():
    st = _form_state(fn_value="")
    llm = FakeLLM(_resp({"type": "FILL", "selector": "#fn", "value": "Jane", "field_label": "First name"}))
    eng = DecisionEngine(llm_client=llm)
    out = await eng.decide(st, "Complete the application")

    assert isinstance(out, ReasoningOutput)
    assert out.observation.page_type == PageType.FORM
    assert out.action.type == ActionType.FILL
    assert out.action.selector == "#fn"
    assert out.action.value == "Jane"
    assert out.valid is True
    assert not any("hallucination" in w for w in out.warnings)
    # screenshot bytes were forwarded to the model
    assert llm.calls[0]["image_bytes"] == st.screenshot
    assert llm.calls[0]["system"]  # a system prompt was supplied


@pytest.mark.asyncio
async def test_hallucinated_selector_is_flagged_invalid():
    st = _form_state()
    llm = FakeLLM(_resp({"type": "FILL", "selector": "#does_not_exist", "value": "x", "field_label": "?"}))
    out = await DecisionEngine(llm_client=llm).decide(st, "fill")
    assert any("hallucination" in w for w in out.warnings)
    assert out.valid is False


@pytest.mark.asyncio
async def test_otp_and_email_signals_surface():
    st = _form_state()
    llm = FakeLLM(_resp(
        {"type": "HANDLE_VERIFICATION"},
        page="VERIFICATION",
        signals={"otp_requested": {"present": True, "evidence": "six code boxes visible"},
                 "email_verification_requested": {"present": True, "evidence": "'check your email' text"}},
    ))
    out = await DecisionEngine(llm_client=llm).decide(st, "verify")
    assert out.signals.otp_requested.present is True
    assert out.signals.email_verification_requested.present is True
    assert out.needs_verification is True
    assert out.action.type == ActionType.HANDLE_VERIFICATION


@pytest.mark.asyncio
async def test_captcha_signal_parsed_with_type():
    st = _form_state(captcha_text="cf-turnstile challenges.cloudflare.com")
    llm = FakeLLM(_resp(
        {"type": "SOLVE_CAPTCHA", "captcha_type": "turnstile"},
        page="CAPTCHA",
        signals={"captcha_present": {"present": True, "detail": "turnstile", "evidence": "turnstile iframe"}},
    ))
    out = await DecisionEngine(llm_client=llm).decide(st, "solve")
    assert out.signals.captcha_present.present is True
    assert out.signals.captcha_present.detail == "turnstile"
    assert out.needs_captcha is True
    # captcha marker IS in the DOM text → no divergence warning
    assert not any("no captcha marker" in w for w in out.warnings)


@pytest.mark.asyncio
async def test_captcha_claim_without_dom_marker_warns():
    st = _form_state(captcha_text="")  # nothing captcha-ish in text/html
    llm = FakeLLM(_resp(
        {"type": "SOLVE_CAPTCHA", "captcha_type": "hcaptcha"},
        signals={"captcha_present": {"present": True, "detail": "hcaptcha", "evidence": "I think I see one"}},
    ))
    out = await DecisionEngine(llm_client=llm).decide(st, "solve")
    assert any("no captcha marker" in w for w in out.warnings)


@pytest.mark.asyncio
async def test_present_signal_without_evidence_warns():
    st = _form_state()
    llm = FakeLLM(_resp(
        {"type": "OBSERVE"},
        signals={"error": {"present": True, "evidence": ""}},  # claim w/o evidence
    ))
    out = await DecisionEngine(llm_client=llm).decide(st, "observe")
    assert any("no evidence" in w for w in out.warnings)


# ── Grounding: SUBMIT / DONE guards ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_with_unfilled_required_is_invalid():
    st = _form_state(fn_value="")  # First name required but empty
    llm = FakeLLM(_resp({"type": "SUBMIT", "selector": "#submit_app"}))
    out = await DecisionEngine(llm_client=llm).decide(st, "submit")
    assert out.valid is False
    assert any("required field" in w for w in out.warnings)


@pytest.mark.asyncio
async def test_submit_when_complete_is_valid():
    st = _form_state(fn_value="Jane")  # all required filled
    llm = FakeLLM(_resp({"type": "SUBMIT", "selector": "#submit_app"}))
    out = await DecisionEngine(llm_client=llm).decide(st, "submit")
    assert out.action.type == ActionType.SUBMIT
    assert out.valid is True


@pytest.mark.asyncio
async def test_done_without_success_message_is_invalid():
    st = _form_state()  # no success message present
    llm = FakeLLM(_resp({"type": "DONE", "confirmation": "looks done"}, page="SUCCESS"))
    out = await DecisionEngine(llm_client=llm).decide(st, "finish")
    assert out.valid is False
    assert any("without a success" in w for w in out.warnings)


@pytest.mark.asyncio
async def test_done_with_success_message_is_valid():
    st = _form_state(success="Thank you for applying! Application received.")
    llm = FakeLLM(_resp({"type": "DONE", "confirmation": "Application received"}, page="SUCCESS"))
    out = await DecisionEngine(llm_client=llm).decide(st, "finish")
    assert out.action.type == ActionType.DONE
    assert out.valid is True


# ── Structural validation ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fill_missing_value_is_invalid():
    st = _form_state()
    llm = FakeLLM(_resp({"type": "FILL", "selector": "#fn"}))  # no value
    out = await DecisionEngine(llm_client=llm).decide(st, "fill")
    assert out.valid is False
    assert any("FILL missing value" in w for w in out.warnings)


@pytest.mark.asyncio
async def test_unknown_action_type_coerces_to_observe():
    st = _form_state()
    llm = FakeLLM(_resp({"type": "FROBNICATE", "selector": "#fn"}))
    out = await DecisionEngine(llm_client=llm).decide(st, "x")
    assert out.action.type == ActionType.OBSERVE


# ── Memory bookkeeping ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_conversation_memory_records_and_backfills():
    st = _form_state(fn_value="")
    # turn 1: FILL. turn 2 (different response): observes the change.
    def responder(n):
        if n == 1:
            return _resp({"type": "FILL", "selector": "#fn", "value": "Jane", "field_label": "First name"})
        return _resp({"type": "FILL", "selector": "#em", "value": "j@x.com", "field_label": "Email"},
                     prev={"had_previous_action": True, "succeeded": True, "evidence": "#fn now shows Jane"})
    # second response also carries a what_changed to backfill turn 1's outcome
    def responder2(n):
        r = responder(n)
        if n == 2:
            r["observation"]["what_changed"] = "First name is now filled with Jane"
        return r

    llm = FakeLLM(responder2)
    eng = DecisionEngine(llm_client=llm)
    conv = ConversationMemory(objective="apply")

    out1 = await eng.decide(st, "apply", conversation=conv, step=1)
    assert len(conv.turns) == 1
    assert conv.turns[0].action_type == "FILL"

    out2 = await eng.decide(st, "apply", conversation=conv, step=2)
    assert len(conv.turns) == 2
    # turn 1 outcome backfilled from turn 2's observation
    assert "First name is now filled" in conv.turns[0].outcome
    assert out2.previous_action.succeeded is True
    # turn 2's prompt saw the previous action
    assert "PREVIOUS ACTION" in llm.calls[1]["prompt"]


# ── Fallback path ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_unavailable_returns_safe_fallback():
    st = _form_state()
    llm = FakeLLM(LLMUnavailable("provider down"))
    conv = ConversationMemory()
    out = await DecisionEngine(llm_client=llm).decide(st, "x", conversation=conv, step=1)
    assert out.action.type == ActionType.OBSERVE
    assert out.valid is False
    assert out.parse_error and "llm_unavailable" in out.parse_error
    # a fallback turn is still recorded so the loop can see it happened
    assert conv.turns and conv.turns[-1].action_type == "OBSERVE"


@pytest.mark.asyncio
async def test_non_dict_response_is_handled():
    st = _form_state()
    llm = FakeLLM(["not", "a", "dict"])
    out = await DecisionEngine(llm_client=llm).decide(st, "x")
    assert out.valid is False
    assert out.parse_error is not None
    assert out.action.type == ActionType.OBSERVE


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
