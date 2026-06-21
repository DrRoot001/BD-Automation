"""Vision-driven page agent.

Wraps a Playwright Page in a thin "what is this page?" / "find me a selector"
layer powered by Gemini. The agent is consulted in two situations:

  1. After navigation, when an adapter needs to know whether the page is an
     application FORM, a job LISTING that requires an Apply click, a SUCCESS
     confirmation, or a BLOCKED bot-protection page.
  2. When an adapter's hardcoded selectors all miss — the agent inspects a
     screenshot + the relevant DOM slice and proposes a new selector. The
     adapter tries it; if it works, the selector is persisted via LearnedFixes.

Every LLM call is optional. If Gemini is not configured the page agent quietly
falls back to "UNKNOWN" page-state and lets the caller proceed on its
existing hardcoded heuristics — the system never blocks on the LLM.
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
You are inspecting the current state of a job-application page. Use the
screenshot AND the DOM snippet to decide.

Return STRICT JSON with this schema:
  {
    "kind": "FORM" | "LISTING" | "SUCCESS" | "BLOCKED" | "ERROR" | "UNKNOWN",
    "confidence": 0.0-1.0,
    "reason": "<= 120 chars",
    "next_action": "click_apply" | "fill_form" | "submit" | "retry" | null,
    "suggested_selector": "<css selector if you can identify the next button>"
                          | null
  }

Definitions:
  FORM     — visible application form with fillable fields.
  LISTING  — job description page; an Apply button is present that must be
             clicked to reveal the form. If so, return next_action="click_apply"
             and suggested_selector pointing at the most visible Apply button.
  SUCCESS  — confirmation that the application was received.
  BLOCKED  — CloudFront/WAF/captcha-wall blocks access. next_action="retry".
  ERROR    — generic error page (404, 500, "this job no longer exists").
  UNKNOWN  — you cannot tell.

For LISTING / FORM, prefer the SHORTEST stable selector that uniquely targets
the next interactive element (id > data-attr > short class chain). Never invent
selectors you cannot see in the DOM snippet.
"""


_SELECTOR_PROMPT = """
You are helping a Playwright automation recover from a missing selector.

The bot tried these selectors and none matched:
  CANDIDATES_TRIED

Channel: CHANNEL (e.g. apply_button, submit, resume_input).

Look at the DOM snippet below and return STRICT JSON:
  {
    "selectors": ["<best>", "<2nd>", "<3rd>"],
    "reason": "<= 120 chars"
  }

Rules:
  1. Each selector must be valid CSS / Playwright syntax.
  2. Prefer #id, [data-qa=...], [aria-label=...] over long class chains.
  3. For text-based clicks use :has-text("Apply") style.
  4. Return 1-3 selectors max, ordered best-first.
  5. Never return a selector that doesn't appear in the DOM snippet.
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
