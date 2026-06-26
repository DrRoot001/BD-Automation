"""Vision-driven page agent — the AI's eyes on the live page.

In this module the AI is the MASTER making dynamic decisions; Playwright is
the SLAVE executing them. The agent is consulted whenever the runner needs
the AI to make a context-aware decision rather than fall back to hardcoded
selectors:

  1. After navigation — "what page did we actually land on?" The AI looks
     at a screenshot + DOM slice and classifies: FORM, LISTING, SUCCESS,
     BLOCKED, ERROR, UNKNOWN. For LISTING / blocked-by-modal cases it also
     tells Playwright the exact selector to click next.
  2. When an adapter's hardcoded selectors all miss — "what should we click
     instead?" The AI proposes 1-3 working selectors from what it can see
     in the DOM. Playwright tries them in order; the winning one is
     persisted via LearnedFixes so the same decision is free next run.

These decisions are NOT recovery hacks — they're the AI exercising its
"navigation power" to handle popups, redirects, unexpected modals, mirror-
site SPAs, and any other context that hardcoded heuristics can't predict.

Every LLM call is optional. If the LLM is unavailable the page agent quietly
falls back to "UNKNOWN" page-state and the caller proceeds on its existing
heuristics — the pipeline never hard-blocks on the LLM.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from playwright.async_api import Frame, Page

from ..llm import LLMUnavailable, get_gemini

logger = logging.getLogger(__name__)


@dataclass
class PageState:
    kind: str   # "FORM", "LISTING", "SUCCESS", "BLOCKED", "ERROR", "UNKNOWN"
    confidence: float
    reason: str
    next_action: Optional[str] = None  # e.g. "click_apply"
    suggested_selector: Optional[str] = None


_VALID_KINDS = {"FORM", "LISTING", "SUCCESS", "BLOCKED", "ERROR", "UNKNOWN"}


_PAGE_PROMPT = """
You are the AI agent driving a real web browser on behalf of a job candidate.
Playwright is your hand — it executes whatever decision you make. Your job
right now is to ANALYZE the page you've landed on and decide what to do next.

Use the screenshot AND the DOM snippet to make your call.

Return STRICT JSON with this schema:
  {
    "kind": "FORM" | "LISTING" | "SUCCESS" | "BLOCKED" | "ERROR" | "UNKNOWN",
    "confidence": 0.0-1.0,
    "reason": "<= 120 chars — why you classified this page that way",
    "next_action": "click_apply" | "fill_form" | "submit" | "retry" | null,
    "suggested_selector": "<the CSS selector Playwright should target next>" | null
  }

How to classify the page:
  FORM     — a visible application form with fillable fields. Tell Playwright
             to start filling: next_action="fill_form".
  LISTING  — a job description page with an Apply button (NO form fields yet).
             Tell Playwright to click Apply: next_action="click_apply" and put
             the most visible Apply button's selector in suggested_selector.
             Common patterns: <a id="apply_button">, <a>Apply for this Job</a>,
             <button>Apply Now</button>. Surface unexpected popups too — if a
             newsletter modal or cookie banner blocks the form, point at its
             close-X selector and set next_action="click_apply".
  SUCCESS  — a confirmation that the application was received. Stop here.
  BLOCKED  — CloudFront/WAF/captcha wall blocks the page. next_action="retry".
  ERROR    — generic error / "this job no longer exists" / 404.
  UNKNOWN  — you cannot tell from what's visible.

Selector rules (you decide, Playwright follows):
  - Pick the SHORTEST stable selector — #id > [data-qa=...] > [aria-label=...]
    > short class chain.
  - Never invent a selector you cannot see in the DOM snippet.
  - For text-based clicks (Apply buttons that lack a clean id) prefer
    a:has-text("Apply for this Job") syntax.
"""


_SELECTOR_PROMPT = """
You are the AI agent driving the browser. Playwright is the slave — it only
executes what you decide. Right now you need to make a DYNAMIC DECISION:
which element on this page should Playwright interact with next.

Context: an earlier attempt tried these selectors and none matched:
  CANDIDATES_TRIED

Your goal channel is: CHANNEL  (e.g. apply_button, submit, resume_input,
popup_close, modal_dismiss).

Look at the DOM snippet + screenshot and DECIDE the selector. Return STRICT
JSON:
  {
    "selectors": ["<best>", "<2nd>", "<3rd>"],
    "reason": "<= 120 chars — why these"
  }

How to decide:
  1. Each selector you propose MUST be valid CSS / Playwright syntax.
  2. Prefer in order: #id  >  [data-qa=...]  >  [aria-label=...]  >
     short class chain. Never propose a long brittle class soup.
  3. For text-driven clicks (Apply buttons, modal close buttons, "I agree"
     etc.) use a:has-text("Apply") or button:has-text("Close") syntax.
  4. If the page has a popup/modal/cookie banner blocking interaction, the
     CORRECT next decision is to target ITS close/accept button — surface
     that selector even if the channel is different.
  5. Return 1–3 selectors, ordered best-first. Playwright will try them
     in order until one works.
  6. Never propose a selector that doesn't actually appear in the DOM
     snippet — that's a hallucination and wastes a turn.
"""


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + " …"


class PageAgent:
    def __init__(self, ats: str = "unknown"):
        self.ats = ats
        self._client = None

    def _llm(self):
        if self._client is None:
            self._client = get_gemini()
        return self._client

    # ─────────────────────────────────────────────────────────────────────────
    # Page state classification
    # ─────────────────────────────────────────────────────────────────────────

    async def classify_page(self, page: Page, frame: Optional[Any] = None) -> PageState:
        """Return the agent's best guess at the current page state."""
        from ..frame_utils import get_live_frame
        try:
            live_frame = await get_live_frame(frame) if frame else None
            ctx = live_frame or page
            # JPEG quality 50 keeps the screenshot small enough for cheap vision
            # while preserving enough detail for page-state classification.
            screenshot = await page.screenshot(
                full_page=False, type="jpeg", quality=50, clip={"x": 0, "y": 0, "width": 1280, "height": 720},
            )
            html = await ctx.content()
            dom = _truncate(html, 5_000)
        except Exception as exc:
            logger.warning(f"[PageAgent] Could not capture page: {exc}")
            return PageState(kind="UNKNOWN", confidence=0.0, reason=f"capture_failed:{exc}")

        try:
            from ..llm import telemetry as _tele
            _tele.set_label("page_agent.call")
            resp = await self._llm().generate_json(
                prompt=_PAGE_PROMPT + "\n\nDOM SNIPPET:\n" + dom,
                image_bytes=screenshot,
                temperature=0.0,
                timeout_s=25.0,
            )
        except LLMUnavailable as exc:
            logger.info(f"[PageAgent] LLM unavailable for classify_page: {exc}")
            return PageState(kind="UNKNOWN", confidence=0.0, reason=str(exc))

        kind = str(resp.get("kind") or "UNKNOWN").upper()
        if kind not in _VALID_KINDS:
            kind = "UNKNOWN"
        return PageState(
            kind=kind,
            confidence=float(resp.get("confidence") or 0.0),
            reason=str(resp.get("reason") or "")[:200],
            next_action=resp.get("next_action") or None,
            suggested_selector=resp.get("suggested_selector") or None,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Selector recovery
    # ─────────────────────────────────────────────────────────────────────────

    async def suggest_selectors(
        self,
        page: Page,
        channel: str,
        tried: List[str],
        frame: Optional[Frame] = None,
        dom_scope_selector: Optional[str] = None,
    ) -> List[str]:
        """Ask Gemini for new selector candidates given the current DOM."""
        from ..frame_utils import get_live_frame
        try:
            live_frame = await get_live_frame(frame) if frame else None
            ctx = live_frame or page
            if dom_scope_selector:
                el = await ctx.query_selector(dom_scope_selector)
                if el:
                    html = await el.inner_html()
                else:
                    html = await ctx.content()
            else:
                html = await ctx.content()
            dom = _truncate(html, 5_000)
            # JPEG quality 50 keeps the screenshot small enough for cheap vision
            # while preserving enough detail for page-state classification.
            screenshot = await page.screenshot(
                full_page=False, type="jpeg", quality=50, clip={"x": 0, "y": 0, "width": 1280, "height": 720},
            )
        except Exception as exc:
            logger.warning(f"[PageAgent] Could not capture for selectors: {exc}")
            return []

        prompt = (
            _SELECTOR_PROMPT
            .replace("CANDIDATES_TRIED", "\n  - " + "\n  - ".join(tried) if tried else "(none)")
            .replace("CHANNEL", channel)
            + "\n\nDOM SNIPPET:\n" + dom
        )
        try:
            from ..llm import telemetry as _tele
            _tele.set_label("page_agent.call")
            resp = await self._llm().generate_json(
                prompt=prompt,
                image_bytes=screenshot,
                temperature=0.0,
                timeout_s=25.0,
            )
        except LLMUnavailable as exc:
            logger.info(f"[PageAgent] LLM unavailable for suggest_selectors: {exc}")
            return []

        out = resp.get("selectors") if isinstance(resp, dict) else None
        if not isinstance(out, list):
            return []
        return [str(s) for s in out if isinstance(s, str) and s.strip()][:3]

    # ─────────────────────────────────────────────────────────────────────────
    # Apply-button finder (combines page state + selector suggestion)
    # ─────────────────────────────────────────────────────────────────────────

    async def find_and_click(
        self,
        page: Page,
        channel: str,
        tried: List[str],
        frame: Optional[Any] = None,
    ) -> Optional[str]:
        """Ask the agent to find a working selector and click it. Returns the
        winning selector, or None if all proposals failed."""
        suggestions = await self.suggest_selectors(page, channel, tried, frame=frame)
        
        # We don't strictly need the live frame here because FrameLocator has .locator(),
        # but to be consistent with ctx we can use frame directly if it's a FrameLocator.
        ctx = frame or page
        for sel in suggestions:
            try:
                loc = ctx.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.scroll_into_view_if_needed()
                    await loc.click(timeout=6000)
                    logger.info(f"[PageAgent] Clicked via learned selector {sel!r}")
                    return sel
            except Exception as exc:
                logger.debug(f"[PageAgent] Learned selector {sel!r} failed: {exc}")
        return None
