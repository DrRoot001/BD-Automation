"""Perception prompt — turns a BrowserState + memory into the LLM messages.

Three pieces:

* :func:`render_browser_state` — a compact, evidence-dense text view of the
  structured :class:`~..perception.BrowserState` (forms, inputs, buttons,
  messages, modals, navigation, stability). This is the DOM evidence the model
  is required to *quote* when it justifies a claim, and it is exactly what the
  engine later validates the model's selectors against.
* :data:`SYSTEM_PROMPT` — the role, the evidence mandate, the fixed question
  set, the action vocabulary, and the strict-JSON schema.
* :func:`build_user_turn` — assembles the per-turn message: objective, browser
  memory, conversation memory (previous action + previous reasoning), and the
  current rendered state. The screenshot is passed separately as image bytes.

The prompt makes exactly ONE demand of the model that the old system did not:
**every yes/no claim and the chosen action must be grounded in something in the
rendered state or the screenshot** — no priors, no "usually it's an OTP screen".
"""
from __future__ import annotations

from typing import List, Optional

from ..perception import BrowserState
from .memory import BrowserMemory, ConversationMemory


# ─────────────────────────────────────────────────────────────────────────────
# BrowserState → evidence text
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_input(i) -> str:
    bits = [i.selector or "?", i.field_type, (i.label or "?")[:70]]
    tags = []
    if i.required:
        tags.append("required")
    if i.disabled:
        tags.append("disabled")
    if i.readonly:
        tags.append("readonly")
    if not i.visible:
        tags.append("hidden")
    line = "  " + " | ".join(bits)
    if tags:
        line += " [" + ",".join(tags) + "]"
    if i.field_type in ("checkbox", "radio"):
        line += f" checked={i.checked}"
    else:
        val = (i.value or "")
        line += f' = "{val[:60]}"' if val else " = (empty)"
    if i.options:
        line += " options=[" + ", ".join(o[:30] for o in i.options[:12]) + "]"
    return line


def render_browser_state(state: BrowserState, *, max_inputs: int = 60, max_buttons: int = 40) -> str:
    """Compact structured view used as the model's DOM evidence."""
    L: List[str] = []
    L.append(f"URL: {state.url}")
    if state.title:
        L.append(f"TITLE: {state.title}")
    nav = state.navigation
    L.append(
        f"NAV: readyState={nav.ready_state or '?'} loading={nav.is_loading} "
        f"frames={nav.frame_count}"
        + (f" (this view is an IFRAME: {state.frame_url})" if state.from_frame else "")
    )
    L.append(f"STABLE: {state.is_stable} ({state.stability_reason})")
    if state.error:
        L.append(f"CAPTURE_NOTE: {state.error}")
    L.append("")

    if state.forms:
        L.append(f"FORMS ({len(state.forms)}):")
        for f in state.forms[:8]:
            L.append(f"  {f.selector} method={f.method or '?'} fields={f.field_count} "
                     f"visible={f.visible}")
        L.append("")

    if state.inputs:
        shown = state.inputs[:max_inputs]
        req = sum(1 for i in state.inputs if i.required)
        req_filled = sum(1 for i in state.inputs if i.required and i.is_filled)
        L.append(f"INPUTS ({len(state.inputs)} total; required {req_filled}/{req} filled):")
        for i in shown:
            L.append(_fmt_input(i))
        if len(state.inputs) > max_inputs:
            L.append(f"  … {len(state.inputs) - max_inputs} more")
        L.append("")

    if state.buttons:
        vis = [b for b in state.buttons if b.visible][:max_buttons]
        L.append(f"BUTTONS ({len(state.buttons)} total, showing visible):")
        for b in vis:
            tags = []
            if b.is_submit:
                tags.append("submit")
            if b.disabled:
                tags.append("disabled")
            t = f" [{','.join(tags)}]" if tags else ""
            L.append(f'  {b.selector} | "{b.text[:50]}"{t}')
        L.append("")

    # Messages grouped by kind so the model can cite the exact banner.
    def _grp(title: str, msgs) -> None:
        if msgs:
            L.append(f"{title}:")
            for m in msgs[:10]:
                assoc = f"  [{m.associated_field}]" if m.associated_field else ""
                L.append(f'  • "{m.text[:140]}"{assoc}')
            L.append("")

    _grp("VALIDATION ERRORS (form rejected input)", state.validation_errors)
    _grp("OTHER ERRORS / ALERTS", [m for m in state.messages if m.kind in ("error", "alert")])
    _grp("SUCCESS / CONFIRMATION MESSAGES", state.success_messages)
    _grp("TOASTS / NOTIFICATIONS", state.toasts)

    if state.modals:
        L.append(f"MODALS / DIALOGS ({len(state.modals)}):")
        for m in state.modals[:6]:
            close = f" close={m.close_selector}" if m.close_selector else " (no close button found)"
            kind = "APPLICATION-FORM modal" if m.looks_like_application else "overlay/popup"
            L.append(f'  {m.selector} [{kind}]{close} — "{m.text_preview[:100]}"')
        L.append("")

    return "\n".join(L).rstrip() or "(empty page)"


# ─────────────────────────────────────────────────────────────────────────────
# System prompt (role + evidence mandate + schema)
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the reasoning core of a browser automation agent applying to a job on \
behalf of a candidate. You do NOT act on assumptions or priors. You act ONLY on \
what you can OBSERVE this turn: the screenshot (image) and the STRUCTURED PAGE \
STATE text. Playwright executes the single action you return — it has no judgment.

════════════════════════════════════════════════════════════════════════════
THE EVIDENCE RULE (this is the whole job)
════════════════════════════════════════════════════════════════════════════
Every claim you make and the action you choose MUST be justified by concrete \
evidence you can point to in the STRUCTURED PAGE STATE or the screenshot. When \
you answer a yes/no question, quote the specific button text, input label, \
message, URL, or visible element that proves it. If you cannot find evidence for \
something, the honest answer is "not present" (present=false) or UNKNOWN — never \
guess based on what "usually" happens.

Forbidden reasoning (all assumption-based):
  • "After a submit there is usually an OTP screen" — only say OTP is requested \
    if you SEE a code-entry field / "enter the code we emailed" text.
  • "No OTP wall means it succeeded" — success requires a POSITIVE confirmation \
    (a success message, a confirmation URL, "application received").
  • "This field is probably the country field" — read its label.
  • "There's usually a captcha here" — only if you SEE a captcha widget/iframe.
  • A HIDDEN "g-recaptcha-response" textarea, an invisible reCAPTCHA v3 badge,
    or a captcha <script> in the markup is NOT a captcha challenge — Greenhouse/
    Lever put these on EVERY form and they resolve silently at submit. Report
    captcha_present=true ONLY for a VISIBLE challenge you must interact with (a
    checkbox, an image grid, a "verify you are human" widget).

════════════════════════════════════════════════════════════════════════════
ANSWER THESE QUESTIONS EVERY TURN (each backed by evidence)
════════════════════════════════════════════════════════════════════════════
  1. What page am I on?              (FORM/LISTING/LOGIN/VERIFICATION/CAPTCHA/
                                      SUCCESS/ERROR/BLOCKED/LOADING/UNKNOWN)
  2. What changed since last turn?   (compare to the previous action + history;
                                      "first observation" if none)
  3. Did the previous action succeed? (true/false/unknown — cite what proves it)
  4. Is there an error on the page?   (validation error, banner, WAF block)
  5. Is an OTP code actually requested? (a code-entry field is visible NOW)
  6. Is email verification requested?   ("check your email", verify-link/code)
  7. Is a CAPTCHA present?              (recaptcha/hcaptcha/turnstile/image)
  8. Is MFA / 2FA requested?           (authenticator app, 2FA, push, security key)
  9. Is a duplicate account / already-applied notice shown?
 10. What is the single next action?
 11. Why? — tie the action to the evidence above.

════════════════════════════════════════════════════════════════════════════
ACTIONS (choose exactly ONE)
════════════════════════════════════════════════════════════════════════════
  OBSERVE                — look again; no page change expected
  WAIT                   — pause for async loading/navigation to finish
  SCROLL {direction}     — "down"/"up" to reveal off-screen content
  CLICK {selector}       — click a non-Apply element (button, checkbox, link)
  CLICK_APPLY {selector} — click an Apply/Apply-Now link to reveal the form
  FILL {selector, value, field_label} — type a value into a text/email/etc field
  SELECT_OPTION {selector, value}     — choose an option in a dropdown/combobox
  UPLOAD {selector, value}            — value is literally "resume" or "cover_letter"
  NEXT_STEP              — click Next/Continue on a multi-step form
  NAVIGATE {url}         — go to a specific URL (last resort; only a URL you can justify)
  SOLVE_CAPTCHA {captcha_type} — hand a visible captcha to the solver
  HANDLE_VERIFICATION    — a code/verification screen is up; the runner fetches
                           the emailed code (you never type or invent a code)
  SUBMIT {selector}      — click the final submit ONLY when every required field
                           is filled and no blocking error/captcha remains
  DONE {confirmation}    — a positive success confirmation is visible; quote it
  ABORT {reason}         — wrong page / hard bot wall / unrecoverable; explain

Rules:
  • ONE action per turn. Prefer selectors that appear in the STRUCTURED PAGE
    STATE — do not invent selectors you cannot see.
  • Never fabricate a value for a field you have no data for; if unsure, pick a
    safer action (OBSERVE/SCROLL) or leave it.
  • Do not SUBMIT while required inputs are unfilled or a validation error /
    captcha is present — fix those first.
  • Do not claim DONE without a positive confirmation message or URL.

════════════════════════════════════════════════════════════════════════════
OUTPUT — return EXACTLY ONE JSON object, no prose, no markdown:
════════════════════════════════════════════════════════════════════════════
{
  "observation": {
    "page_type": "FORM|LISTING|LOGIN|VERIFICATION|CAPTCHA|SUCCESS|ERROR|BLOCKED|LOADING|UNKNOWN",
    "page_evidence": "<what on the page proves this classification>",
    "what_changed": "<diff vs the previous action/turn, or 'first observation'>",
    "change_evidence": "<what proves the change>"
  },
  "previous_action_assessment": {
    "had_previous_action": true|false,
    "succeeded": true|false|null,
    "evidence": "<what proves success/failure, or why it's unknown>"
  },
  "signals": {
    "error":                        {"present": true|false, "detail": "<text>", "evidence": "<quote>"},
    "otp_requested":                {"present": true|false, "detail": "", "evidence": "<quote>"},
    "email_verification_requested": {"present": true|false, "detail": "", "evidence": "<quote>"},
    "captcha_present":              {"present": true|false, "detail": "recaptcha_v2|hcaptcha|turnstile|image", "evidence": "<quote>"},
    "mfa_requested":                {"present": true|false, "detail": "2fa|authenticator|push|security_key", "evidence": "<quote>"},
    "duplicate_account":            {"present": true|false, "detail": "", "evidence": "<quote>"}
  },
  "decision": {
    "action": {"type": "<ACTION>", "selector": "", "value": "", "field_label": "",
               "url": "", "direction": "", "captcha_type": "", "confirmation": "", "reason": ""},
    "why": "<justify the action using the evidence above>",
    "confidence": 0.0-1.0
  }
}
Include only the action params relevant to the chosen action; leave the rest "".
"""


# ─────────────────────────────────────────────────────────────────────────────
# Per-turn user message
# ─────────────────────────────────────────────────────────────────────────────

def build_user_turn(
    state: BrowserState,
    objective: str,
    *,
    browser_memory: Optional[BrowserMemory] = None,
    conversation: Optional[ConversationMemory] = None,
    step: Optional[int] = None,
    max_steps: Optional[int] = None,
) -> str:
    """Assemble the per-turn user message from state + objective + memory."""
    L: List[str] = []
    if step is not None:
        hdr = f"TURN {step}"
        if max_steps:
            hdr += f"/{max_steps}"
        L.append(hdr)

    L.append("CURRENT OBJECTIVE:")
    L.append("  " + (objective or "Complete and submit this job application.").strip())
    L.append("")

    # Previous action + previous reasoning (explicit, from conversation memory).
    prev = conversation.last if conversation else None
    if prev is not None:
        L.append("PREVIOUS ACTION (yours, last turn):")
        L.append("  " + (prev.action_summary or prev.action_type))
        if prev.reasoning_summary:
            L.append("PREVIOUS REASONING:")
            L.append("  " + prev.reasoning_summary[:300])
        L.append("")

    if conversation and conversation.turns:
        L.append("CONVERSATION MEMORY (recent turns this session):")
        L.append(conversation.render())
        L.append("")

    if browser_memory is not None:
        L.append("BROWSER MEMORY (what we know about this portal from prior runs):")
        L.append(browser_memory.render())
        L.append("")

    L.append("STRUCTURED PAGE STATE (observed NOW — your evidence; screenshot also attached):")
    L.append(render_browser_state(state))
    L.append("")
    L.append("Answer the 9 questions and return the single JSON object per the schema.")
    return "\n".join(L)
