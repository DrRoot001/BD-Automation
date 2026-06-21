"""Vision-driven agent loop for autonomous form filling.

The loop replaces the fixed script with a continuous perception → decision →
action cycle. On every step the agent receives a screenshot + scoped DOM and
returns ONE structured action. Playwright executes it, then the loop repeats.

Supported actions
-----------------
  verify_page   — first step; confirm this is a job application form
  fill_field    — type / select / check a value into a field
  upload_file   — attach resume or cover letter
  click         — click any element (buttons, checkboxes, Apply links)
  scroll        — scroll the viewport to reveal off-screen content
  next_step     — click "Next" / "Continue" on multi-step forms
  wait          — short pause (e.g. after a dialog opens)
  abort         — give up with a reason (wrong page, bot wall, etc.)
  done          — application submitted; include confirmation text

Design constraints
------------------
- Max MAX_STEPS iterations to prevent runaway loops.
- STUCK_THRESHOLD consecutive steps with no DOM change → abort.
- Every action is logged with its step index for debugging.
- LLM errors are non-fatal for up to LLM_RETRY_LIMIT retries, then abort.
- File uploads always use local paths resolved by the executor.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from playwright.async_api import Frame, Page

from ..adapters.hints import format_hints_for_prompt, get_platform_hints
from ..llm import LLMUnavailable, get_llm

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────

MAX_STEPS = 60          # hard cap on loop iterations
STUCK_THRESHOLD = 4     # consecutive no-DOM-change steps before abort
LLM_RETRY_LIMIT = 3     # consecutive LLM failures before abort
STEP_TIMEOUT_S = 30.0   # per-step LLM call timeout
DOM_HASH_SELECTOR = "body"  # element used to detect DOM changes between steps


# ─────────────────────────────────────────────────────────────────────────────
# Action schema
# ─────────────────────────────────────────────────────────────────────────────

ActionKind = Literal[
    "verify_page",
    "fill_field",
    "upload_file",
    "click",
    "scroll",
    "next_step",
    "wait",
    "abort",
    "done",
]


@dataclass
class AgentAction:
    kind: ActionKind
    # fill_field / upload_file
    selector: Optional[str] = None
    value: Optional[str] = None          # text value or "resume" / "cover_letter"
    field_label: Optional[str] = None    # human label for logging
    # scroll
    direction: Optional[Literal["down", "up"]] = None
    # abort / done
    reason: Optional[str] = None
    confirmation: Optional[str] = None
    # click
    click_text: Optional[str] = None     # :has-text fallback if selector fails
    # internal
    step: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LoopResult:
    success: bool
    status: Literal[
        "SUBMITTED", "FORM_COMPLETED", "ABORTED", "MAX_STEPS",
        "STUCK", "LLM_UNAVAILABLE", "WRONG_PAGE", "ERROR"
    ]
    confirmation: Optional[str] = None
    error: Optional[str] = None
    steps_taken: int = 0
    actions: List[AgentAction] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Prompt templates
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are an autonomous job-application agent controlling a web browser.
Your SOLE task: fill out the job application form and submit it on behalf of the candidate.
If you see a newsletter signup or contact form (e.g. in a page footer), IGNORE IT and focus on the job application fields. Do not abort just because a newsletter form is visible on the page, as long as the page contains a job application.

On every turn you receive:
  - A screenshot of the current viewport
  - A DOM snapshot of interactive elements
  - Candidate profile (name, email, skills, etc.)
  - Job context (title, company, platform)
  - History of actions already taken this session
  - A list of fields still needing to be filled

You respond with EXACTLY ONE action as a JSON object. No prose, no markdown fences.

Action schema:
{
  "kind": "verify_page" | "fill_field" | "upload_file" | "click" | "scroll" |
          "next_step" | "wait" | "abort" | "done",

  // fill_field:
  "selector": "<css selector>",
  "value": "<value to type or select>",
  "field_label": "<human label for logging>",

  // upload_file:
  "selector": "<file input css selector>",
  "value": "resume" | "cover_letter",
  "field_label": "<label>",

  // click:
  "selector": "<css selector>",
  "click_text": "<fallback :has-text() string if selector fails>",

  // scroll:
  "direction": "down" | "up",

  // next_step: no extra fields needed

  // wait: no extra fields needed

  // abort:
  "reason": "<why you are giving up>",

  // done:
  "confirmation": "<confirmation text visible on screen, or 'submitted'>",

  // verify_page (first step only):
  // return kind="verify_page" — if page is a genuine job application form, also
  // include "selector" pointing at the first fillable field so the next step
  // can begin. If it is NOT a job application form, return kind="abort" instead.
}

Rules:
1. verify_page MUST be the very first action. Abort immediately if not a job application.
2. fill_field: selector must point to the actual <input>/<select>/<textarea> element.
3. For react-select / custom dropdowns: use the combobox input id (e.g. #react-select-X-input).
4. upload_file value: "resume" or "cover_letter" — never a path string.
5. After filling ALL visible fields, look for a Next/Continue button → next_step.
6. After final page, look for Submit/Apply → click it, then return done.
7. If you see a CAPTCHA you cannot solve, return abort with reason="captcha_wall".
8. If the page looks like a login wall or bot detection, return abort with reason="blocked".
9. If the same field keeps appearing unfilled after 2 fill attempts, skip it and continue.
10. Prefer id selectors (#foo) over class chains. Never invent selectors not in the DOM.
"""

_USER_TURN_TEMPLATE = """=== STEP {step} / {max_steps} ===

CANDIDATE:
{candidate_json}

JOB:
{job_json}

PLATFORM HINTS ({platform}):
{platform_hints}

FIELDS ALREADY FILLED THIS SESSION:
{filled_summary}

ACTIONS TAKEN SO FAR ({n_actions}):
{history}

DOM SNAPSHOT (interactive elements only):
{dom_snapshot}

Based on the screenshot and DOM above, return your next single action as JSON.
Do NOT re-fill fields already listed in FIELDS ALREADY FILLED. Focus on empty or unfilled fields.
"""


# ─────────────────────────────────────────────────────────────────────────────
# DOM snapshot helper — extracts only interactive elements to keep prompt small
# ─────────────────────────────────────────────────────────────────────────────

_DOM_SNAPSHOT_JS = """() => {
    const MAX_FIELDS = 40;
    const out = [];
    const seen = new Set();
    const skip_types = new Set(['hidden', 'submit', 'button', 'image', 'reset']);

    // Skip phone-widget containers
    function inPhoneWidget(el) {
        return !!el.closest('.iti, .iti__country-list, .iti--container');
    }

    // Resolve a human label from nearby DOM
    function labelFor(el) {
        if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim().slice(0, 80);
        const id = el.id;
        if (id) {
            const lbl = document.querySelector('label[for="' + id + '"]');
            if (lbl) return lbl.textContent.trim().slice(0, 80);
        }
        let p = el.parentElement;
        for (let i = 0; i < 5 && p; i++) {
            const lbl = p.querySelector(':scope > label, :scope > legend, :scope > span.label');
            if (lbl && !lbl.contains(el)) return lbl.textContent.trim().slice(0, 80);
            p = p.parentElement;
        }
        return el.placeholder || el.name || el.id || '?';
    }

    // Native inputs / selects / textareas
    document.querySelectorAll('input, select, textarea').forEach(el => {
        if (out.length >= MAX_FIELDS) return;
        const type = (el.type || 'text').toLowerCase();
        if (skip_types.has(type)) return;
        if (inPhoneWidget(el)) return;
        const style = window.getComputedStyle(el);
        if (style.display === 'none') return;
        if (style.visibility === 'hidden' && type !== 'radio' && type !== 'checkbox') return;
        // Build selector
        const sel = el.id ? '#' + el.id : (el.name ? el.tagName.toLowerCase() + '[name="' + el.name + '"]' : el.tagName.toLowerCase());
        if (seen.has(sel)) return;
        seen.add(sel);
        const entry = { sel, type, label: labelFor(el), required: el.required || el.getAttribute('aria-required') === 'true' };
        if (el.tagName === 'SELECT') {
            const opts = Array.from(el.options).filter(o => o.value).map(o => o.text.trim()).slice(0, 15);
            if (opts.length) entry.options = opts;
        }
        out.push(entry);
    });

    // Custom dropdowns (react-select combobox inputs)
    document.querySelectorAll('[role="combobox"]').forEach(el => {
        if (out.length >= MAX_FIELDS) return;
        if (inPhoneWidget(el)) return;
        const sel = el.id ? '#' + el.id : null;
        if (!sel || seen.has(sel)) return;
        seen.add(sel);
        out.push({ sel, type: 'combobox', label: labelFor(el), required: false });
    });

    // Visible buttons (Next / Submit / Apply)
    document.querySelectorAll('button, input[type="submit"], input[type="button"]').forEach(el => {
        if (out.length >= MAX_FIELDS) return;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return;
        const text = el.textContent.trim() || el.value || '';
        if (!text) return;
        const sel = el.id ? '#' + el.id : 'button:has-text("' + text.slice(0, 40) + '")';
        if (seen.has(sel)) return;
        seen.add(sel);
        out.push({ sel, type: 'button', label: text.slice(0, 80) });
    });

    return out;
}"""


async def _dom_snapshot(page: Page, frame: Optional[Frame] = None, is_iframe_mode: bool = False) -> str:
    # If we are in iframe mode but frame is None (e.g. detached mid-loop), we MUST NOT fall back to page,
    # otherwise we capture the top-level site's DOM instead of the form.
    if is_iframe_mode and not frame:
        return "(iframe detached or reloading)"
    ctx = frame or page
    try:
        fields = await ctx.evaluate(_DOM_SNAPSHOT_JS)
        lines = []
        for f in fields or []:
            req = " [required]" if f.get("required") else ""
            opts = f" options={f['options']}" if f.get("options") else ""
            lines.append(f"  {f['sel']} | {f['type']} | {f.get('label','?')}{req}{opts}")
        return "\n".join(lines) or "(no interactive elements found)"
    except Exception as exc:
        logger.warning(f"[AgentLoop] dom_snapshot failed: {exc}")
        return "(dom snapshot unavailable)"


async def _dom_hash(ctx) -> str:
    """SHA1 of the body inner HTML and input values — used to detect DOM changes between steps."""
    try:
        html = await ctx.evaluate("""() => {
            let html = document.body.innerHTML;
            let vals = Array.from(document.querySelectorAll('input, select, textarea')).map(e => e.value).join('|');
            return html + vals;
        }""")
        return hashlib.sha1(html.encode()).hexdigest()[:16]
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Action parser
# ─────────────────────────────────────────────────────────────────────────────

_VALID_KINDS: set[str] = {
    "verify_page", "fill_field", "upload_file", "click",
    "scroll", "next_step", "wait", "abort", "done",
}


def _parse_action(raw: Dict[str, Any], step: int) -> Optional[AgentAction]:
    kind = str(raw.get("kind") or "").strip()
    if kind not in _VALID_KINDS:
        logger.warning(f"[AgentLoop] step={step} unknown action kind={kind!r}")
        return None
    return AgentAction(
        kind=kind,  # type: ignore[arg-type]
        selector=raw.get("selector") or None,
        value=raw.get("value") or None,
        field_label=raw.get("field_label") or None,
        direction=raw.get("direction") or None,
        reason=raw.get("reason") or None,
        confirmation=raw.get("confirmation") or None,
        click_text=raw.get("click_text") or None,
        step=step,
        raw=raw,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Action executor
# ─────────────────────────────────────────────────────────────────────────────

async def _execute_action(
    action: AgentAction,
    page: Page,
    frame: Optional[Frame],
    resume_path: Optional[str],
    cover_letter_path: Optional[str],
) -> bool:
    """Execute one action. Returns True if Playwright succeeded."""
    ctx = frame or page
    kind = action.kind

    if kind in ("verify_page", "wait"):
        await asyncio.sleep(0.5)
        return True

    if kind == "scroll":
        direction = action.direction or "down"
        delta = 600 if direction == "down" else -600
        await page.evaluate(f"window.scrollBy(0, {delta})")
        await asyncio.sleep(0.3)
        return True

    if kind == "next_step":
        # Try common next/continue selectors
        candidates = [
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "button:has-text('Next Step')",
            "[data-qa='btn-next']",
            "button[type='button']:has-text('Next')",
        ]
        for sel in candidates:
            try:
                loc = ctx.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.scroll_into_view_if_needed()
                    await loc.click(timeout=6000)
                    await asyncio.sleep(1.0)
                    return True
            except Exception:
                continue
        logger.warning("[AgentLoop] next_step: no Next button found")
        return False

    if kind == "click":
        sel = action.selector
        text = action.click_text
        tried: List[str] = []
        if sel:
            tried.append(sel)
        if text:
            tried.append(f"button:has-text('{text}')")
            tried.append(f":has-text('{text}')")
        for s in tried:
            try:
                loc = ctx.locator(s).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.scroll_into_view_if_needed()
                    await loc.click(timeout=6000)
                    await asyncio.sleep(0.8)
                    return True
            except Exception:
                continue
        logger.warning(f"[AgentLoop] click: no element matched sel={sel!r} text={text!r}")
        return False

    if kind == "upload_file":
        file_key = (action.value or "").lower()
        path = resume_path if "cover" not in file_key else cover_letter_path
        if not path:
            logger.warning(f"[AgentLoop] upload_file: no path for key={file_key!r}")
            return False
        sel = action.selector
        if not sel:
            logger.warning("[AgentLoop] upload_file: no selector provided")
            return False
        try:
            loc = ctx.locator(sel).first
            await loc.set_input_files(path, timeout=10000)
            await asyncio.sleep(0.5)
            return True
        except Exception as exc:
            logger.warning(f"[AgentLoop] upload_file failed: {exc}")
            return False

    if kind == "fill_field":
        sel = action.selector
        value = action.value or ""
        if not sel:
            logger.warning("[AgentLoop] fill_field: no selector")
            return False
        try:
            loc = ctx.locator(sel).first
            if await loc.count() == 0:
                logger.warning(f"[AgentLoop] fill_field: selector not found: {sel!r}")
                return False
            field_type = await loc.evaluate("el => el.type || el.tagName.toLowerCase()")
            field_type = (field_type or "text").lower()

            if field_type == "file":
                # Redirect to upload logic
                path = resume_path
                if "cover" in (action.field_label or "").lower():
                    path = cover_letter_path
                if not path:
                    return False
                await loc.set_input_files(path, timeout=10000)
                return True

            if field_type in ("checkbox",):
                if value.lower() in ("yes", "true", "1", "on", "agree"):
                    await loc.check(force=True, timeout=5000)
                return True

            if field_type == "radio":
                await loc.check(force=True, timeout=5000)
                return True

            if field_type == "select-one" or field_type == "select":
                try:
                    await loc.select_option(label=value, timeout=5000)
                    return True
                except Exception:
                    try:
                        await loc.select_option(value=value, timeout=3000)
                        return True
                    except Exception:
                        return False

            # combobox (react-select) — type into the search input
            if field_type == "combobox" or "combobox" in (await loc.get_attribute("role") or ""):
                await loc.click(timeout=5000)
                await asyncio.sleep(0.3)
                await loc.fill("")
                await loc.type(value, delay=40)
                await asyncio.sleep(0.5)
                # Try to click the first matching option
                option_sel = f".select__option:has-text('{value}'), [role='option']:has-text('{value}')"
                try:
                    opt = ctx.locator(option_sel).first
                    if await opt.count() > 0:
                        await opt.click(timeout=3000)
                        return True
                except Exception:
                    pass
                await page.keyboard.press("Enter")
                return True

            # Default: text / email / textarea / url / tel / number
            try:
                await loc.scroll_into_view_if_needed(timeout=3000)
                await loc.wait_for(state="visible", timeout=3000)
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.05, 0.15))
            # Use native setter to handle React-controlled inputs
            committed = await loc.evaluate(
                """(el, v) => {
                    try {
                        const proto = el.tagName === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                        setter.call(el, v);
                        el.dispatchEvent(new Event('input',  {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        return el.value === v;
                    } catch(e) { return false; }
                }""",
                value,
            )
            if not committed:
                await loc.fill(value, timeout=5000)
            await asyncio.sleep(random.uniform(0.1, 0.3))
            return True

        except Exception as exc:
            logger.warning(f"[AgentLoop] fill_field sel={sel!r} value={value!r} error: {exc}")
            return False

    return False


# ─────────────────────────────────────────────────────────────────────────────
# History formatter
# ─────────────────────────────────────────────────────────────────────────────

def _format_history(actions: List[AgentAction], last_n: int = 12) -> str:
    if not actions:
        return "(none)"
    recent = actions[-last_n:]
    lines = []
    for a in recent:
        if a.kind == "fill_field":
            lines.append(f"  [{a.step}] fill_field  '{a.field_label or a.selector}' = '{(a.value or '')[:60]}'")
        elif a.kind == "upload_file":
            lines.append(f"  [{a.step}] upload_file '{a.field_label or a.selector}' ({a.value})")
        elif a.kind == "click":
            lines.append(f"  [{a.step}] click       sel={a.selector!r} text={a.click_text!r}")
        elif a.kind == "scroll":
            lines.append(f"  [{a.step}] scroll      {a.direction}")
        elif a.kind == "next_step":
            lines.append(f"  [{a.step}] next_step")
        elif a.kind == "verify_page":
            lines.append(f"  [{a.step}] verify_page ✓")
        else:
            lines.append(f"  [{a.step}] {a.kind}  {a.reason or a.confirmation or ''}")
    return "\n".join(lines)


def _compact(obj: Any, max_keys: int = 12) -> str:
    """JSON-encode an object, capping dict entries to keep prompt size down."""
    if isinstance(obj, dict):
        trimmed = dict(list(obj.items())[:max_keys])
        return json.dumps(trimmed, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _filled_summary(actions: List[AgentAction]) -> str:
    """One-line-per-field summary of what's already been filled this session.

    Tells the LLM not to refill fields it already handled, preventing the most
    common stuck pattern: agent sees empty field, fills it, DOM updates, sees it
    again on the same page with a slightly different label and fills it twice.
    """
    lines = []
    for a in actions:
        if a.kind == "fill_field" and a.field_label and a.value:
            lines.append(f"  {a.field_label}: {str(a.value)[:60]}")
        elif a.kind == "upload_file" and a.field_label:
            lines.append(f"  {a.field_label}: [file uploaded — {a.value}]")
    return "\n".join(lines) if lines else "(none yet)"


# ─────────────────────────────────────────────────────────────────────────────
# Main agent loop
# ─────────────────────────────────────────────────────────────────────────────

class AgentLoop:
    """Autonomous vision-driven form-filling agent.

    Usage::

        loop = AgentLoop(
            candidate_profile=profile_dict,
            job_context={"title": "SWE", "company": "Acme", "platform": "greenhouse"},
            resume_path="/tmp/resume.pdf",
            cover_letter_path=None,
        )
        result = await loop.run(page, frame=adapter._frame)
    """

    def __init__(
        self,
        candidate_profile: Dict[str, Any],
        job_context: Dict[str, Any],
        resume_path: Optional[str] = None,
        cover_letter_path: Optional[str] = None,
        max_steps: int = MAX_STEPS,
        platform_hints: Optional[Dict[str, Any]] = None,
    ):
        self.profile = candidate_profile
        self.job_ctx = job_context
        self.resume_path = resume_path
        self.cover_letter_path = cover_letter_path
        self.max_steps = max_steps
        # Auto-populate hints from registry if not explicitly provided
        platform = str(job_context.get("platform") or "generic").lower()
        self._hints = platform_hints if platform_hints is not None else get_platform_hints(platform)
        self._platform = platform
        self._llm = get_llm()

    async def run(
        self,
        page: Page,
        frame: Optional[Any] = None,
    ) -> LoopResult:
        from ..frame_utils import get_live_frame

        actions: List[AgentAction] = []
        stuck_count = 0
        llm_error_count = 0
        prev_dom_hash = ""
        page_verified = False
        is_iframe_mode = frame is not None

        transition_state = {"is_transitioning": False}

        def on_frame_attached(f: Any):
            import time
            if f == page.main_frame or getattr(f, "parent_frame", None) == page.main_frame:
                logger.info(f"[FrameLifecycle {time.time():.3f}] attached: {f.name}")

        def on_frame_navigated(f: Any):
            import time
            if f == page.main_frame or getattr(f, "parent_frame", None) == page.main_frame:
                logger.info(f"[FrameLifecycle {time.time():.3f}] navigated: {f.name} (url: {f.url})")
                transition_state["is_transitioning"] = True

        def on_frame_detached(f: Any):
            import time
            if f == page.main_frame or getattr(f, "parent_frame", None) == page.main_frame:
                logger.info(f"[FrameLifecycle {time.time():.3f}] detached: {f.name}")
                transition_state["is_transitioning"] = True

        page.on("frameattached", on_frame_attached)
        page.on("framenavigated", on_frame_navigated)
        page.on("framedetached", on_frame_detached)

        try:
            for step in range(1, self.max_steps + 1):
                if transition_state["is_transitioning"]:
                    from ..frame_utils import wait_for_stability
                    await wait_for_stability(page, frame, is_iframe_mode)
                    transition_state["is_transitioning"] = False

                # ── Check frame attachment ──
                live_frame = None
                if is_iframe_mode:
                    live_frame = await get_live_frame(frame)
                    if live_frame and live_frame.is_detached():
                        logger.warning(f"[AgentLoop] step={step} Frame is detached!")
                        live_frame = None

                # ── Capture perception ──────────────────────────────────────────
                try:
                    screenshot = await page.screenshot(
                        full_page=False,
                        type="jpeg",
                        quality=55,
                        clip={"x": 0, "y": 0, "width": 1280, "height": 800},
                    )
                    dom = await _dom_snapshot(page, live_frame, is_iframe_mode)
                    current_hash = await _dom_hash(live_frame or page)
                except Exception as exc:
                    logger.error(f"[AgentLoop] step={step} capture failed: {exc}")
                    return LoopResult(
                        success=False,
                        status="ERROR",
                        error=f"screenshot/dom capture failed: {exc}",
                        steps_taken=step,
                        actions=actions,
                    )

                # ── Stuck detector ──────────────────────────────────────────────
                if current_hash and current_hash == prev_dom_hash and step > 1:
                    stuck_count += 1
                    if stuck_count >= STUCK_THRESHOLD:
                        logger.warning(f"[AgentLoop] stuck for {stuck_count} steps — aborting")
                        return LoopResult(
                            success=False,
                            status="STUCK",
                            error=f"DOM unchanged for {stuck_count} consecutive steps",
                            steps_taken=step,
                            actions=actions,
                        )
                else:
                    stuck_count = 0
                prev_dom_hash = current_hash

                # ── Build prompt ────────────────────────────────────────────────
                # Keep profile compact — drop heavy fields the agent doesn't need
                safe_profile = {
                    k: v for k, v in self.profile.items()
                    if k not in ("embedding", "google_refresh_token", "resume_parsed_json")
                    and v not in (None, "", [])
                }
                safe_job = {k: v for k, v in self.job_ctx.items() if v not in (None, "")}

                user_msg = _USER_TURN_TEMPLATE.format(
                    step=step,
                    max_steps=self.max_steps,
                    candidate_json=_compact(safe_profile),
                    job_json=_compact(safe_job, max_keys=8),
                    platform=self._platform,
                    platform_hints=format_hints_for_prompt(self._hints),
                    filled_summary=_filled_summary(actions),
                    n_actions=len(actions),
                    history=_format_history(actions),
                    dom_snapshot=dom,
                )

                # ── LLM call ────────────────────────────────────────────────────
                try:
                    raw = await self._llm.generate_json(
                        prompt=user_msg,
                        image_bytes=screenshot,
                        temperature=0.0,
                        timeout_s=STEP_TIMEOUT_S,
                        system=_SYSTEM_PROMPT,
                    )
                    llm_error_count = 0
                except LLMUnavailable as exc:
                    llm_error_count += 1
                    logger.warning(f"[AgentLoop] step={step} LLM error ({llm_error_count}/{LLM_RETRY_LIMIT}): {exc}")
                    if llm_error_count >= LLM_RETRY_LIMIT:
                        return LoopResult(
                            success=False,
                            status="LLM_UNAVAILABLE",
                            error=str(exc),
                            steps_taken=step,
                            actions=actions,
                        )
                    await asyncio.sleep(2.0)
                    continue

                # ── Parse action ────────────────────────────────────────────────
                if not isinstance(raw, dict):
                    logger.warning(f"[AgentLoop] step={step} LLM returned non-dict: {type(raw)}")
                    continue

                action = _parse_action(raw, step)
                if action is None:
                    continue

                logger.info(
                    f"[AgentLoop] step={step}/{self.max_steps} "
                    f"kind={action.kind} "
                    f"sel={action.selector!r} "
                    f"val={(action.value or '')[:50]!r} "
                    f"label={(action.field_label or '')[:50]!r}"
                )

                # ── Terminal actions ─────────────────────────────────────────────
                if action.kind == "abort":
                    reason = action.reason or "agent aborted"
                    logger.warning(f"[AgentLoop] ABORT: {reason}")
                    status = "WRONG_PAGE" if "newsletter" in reason.lower() or "wrong" in reason.lower() else "ABORTED"
                    return LoopResult(
                        success=False,
                        status=status,  # type: ignore[arg-type]
                        error=reason,
                        steps_taken=step,
                        actions=actions,
                    )

                if action.kind == "done":
                    actions.append(action)
                    logger.info(f"[AgentLoop] DONE after {step} steps: {action.confirmation!r}")
                    return LoopResult(
                        success=True,
                        status="SUBMITTED",
                        confirmation=action.confirmation,
                        steps_taken=step,
                        actions=actions,
                    )

                # ── verify_page gate ─────────────────────────────────────────────
                if action.kind == "verify_page":
                    page_verified = True
                    actions.append(action)
                    logger.info("[AgentLoop] page verified as job application form")
                    continue

                # Safety: if agent never verified the page by step 3, abort
                if not page_verified and step >= 3:
                    logger.warning("[AgentLoop] page never verified — aborting to prevent wrong-page fill")
                    return LoopResult(
                        success=False,
                        status="WRONG_PAGE",
                        error="Agent did not verify page within 3 steps",
                        steps_taken=step,
                        actions=actions,
                    )

                # ── Execute action ───────────────────────────────────────────────
                ok = await _execute_action(
                    action, page, frame, self.resume_path, self.cover_letter_path
                )
                if not ok:
                    logger.debug(f"[AgentLoop] step={step} action {action.kind} returned ok=False")

                actions.append(action)

            # ── Max steps reached ────────────────────────────────────────────────
            logger.warning(f"[AgentLoop] reached MAX_STEPS={self.max_steps}")
            return LoopResult(
                success=False,
                status="MAX_STEPS",
                error=f"Exceeded {self.max_steps} steps without completing",
                steps_taken=self.max_steps,
                actions=actions,
            )
        finally:
            page.remove_listener("frameattached", on_frame_attached)
            page.remove_listener("framenavigated", on_frame_navigated)
            page.remove_listener("framedetached", on_frame_detached)
