"""Generic fallback adapter — used when no platform-specific adapter matches.

Best-effort filling for unknown ATS platforms. Uses learned_fixes for the
Apply and Submit channels so even unknown platforms benefit from the
cross-run learning loop.
"""
from __future__ import annotations

import logging
import asyncio
from typing import Optional, Tuple

from playwright.async_api import Page

from .base import BasePlatformAdapter
from ..agent import get_learned_fixes
from ..forms import detect_form, fill_form, upload_file

logger = logging.getLogger(__name__)


_APPLY_SELECTORS = [
    "a:has-text('Apply for this job')",
    "a:has-text('Apply Now')",
    "a:has-text('Apply')",
    "button:has-text('Apply for this job')",
    "button:has-text('Apply Now')",
    "button:has-text('Apply')",
]

_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Submit application')",
    "button:has-text('Submit Application')",
    "button:has-text('Send Application')",
    "button:has-text('Send application')",
    "button:has-text('Submit')",
    "button:has-text('Apply now')",
    "button:has-text('Apply Now')",
    "button:has-text('Apply')",
    "input[type='button'][value*='ubmit']",
    "[role='button']:has-text('Submit')",
    "[data-qa*='submit' i]",
    "[data-testid*='submit' i]",
    "button[class*='submit' i]",
]

# Multi-step forms (Jobvite, SmartRecruiters, …) hide Submit behind one or
# more Next/Continue pages. Bounded progression through them beats bailing.
_NEXT_STEP_SELECTORS = [
    "button:has-text('Next')",
    "button:has-text('Continue')",
    "button:has-text('Review')",
    "[role='button']:has-text('Next')",
    "input[type='button'][value='Next']",
]

# Specific confirmation phrases only. A bare "thank you" matches page footers,
# cookie notices and newsletter confirmations on almost any site and caused
# false-positive SUBMITTED records — it is deliberately excluded.
_SUCCESS_PATTERNS = (
    "thank you for applying",
    "thank you for your application",
    "application received",
    "successfully submitted",
    "application complete",
    "your application has been",
    "we've received your application",
    "we have received your application",
)


class GenericFormAdapter(BasePlatformAdapter):
    platform_name = "generic"

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=25_000)
        except Exception as exc:
            logger.warning(f"[Generic] navigate timeout: {exc}")
        await self.human_delay(0.8, 1.6)

        # If there are no form inputs visible, try clicking an Apply button.
        try:
            await page.locator("input, textarea, select").first.wait_for(state="attached", timeout=2_500)
            return
        except Exception:
            pass

        learned = get_learned_fixes("generic").get("apply_button")
        for sel in learned + [s for s in _APPLY_SELECTORS if s not in learned]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=5_000)
                    await page.wait_for_load_state("domcontentloaded", timeout=12_000)
                    get_learned_fixes("generic").add("apply_button", sel)
                    break
            except Exception:
                continue

    async def detect_application_type(self, page: Page) -> str:
        form = await detect_form(page, container_selector=self.container_selector)
        return form.form_type

    async def fill_application(
        self,
        page: Page,
        profile: dict,
        resume_path: str,
        cover_letter_path: Optional[str],
        screening_answers: Optional[dict],
        pre_detected_form=None, candidate_id=None,
    ) -> bool:
        form = pre_detected_form or await detect_form(page, container_selector=self.container_selector)
        fill_success = await fill_form(page, form, profile, screening_answers, candidate_id=candidate_id)

        for field in form.fields:
            if field.field_type != "file":
                continue
            lbl = (field.label or "").lower()
            if cover_letter_path and "cover" in lbl:
                await upload_file(page, field.selector, cover_letter_path)
            elif "resume" in lbl or "cv" in lbl or not any(k in lbl for k in ("cover", "other")):
                await upload_file(page, field.selector, resume_path)

        return fill_success

    def _search_contexts(self, page: Page):
        """Main page first, then child frames — embedded ATS forms (Greenhouse
        boards, Jobvite widgets) live in iframes the page-level locator misses."""
        contexts = [page]
        try:
            contexts += [f for f in page.frames if f is not page.main_frame]
        except Exception:
            pass
        return contexts

    async def _click_first_actionable(self, ctx, selectors) -> Optional[str]:
        """Click the first VISIBLE and ENABLED match; `.first` alone often hits
        a hidden template node and burns the whole click timeout."""
        for sel in selectors:
            try:
                loc = ctx.locator(sel)
                for i in range(min(await loc.count(), 5)):
                    btn = loc.nth(i)
                    try:
                        if not await btn.is_visible() or not await btn.is_enabled():
                            continue
                        await btn.scroll_into_view_if_needed()
                        await btn.click(timeout=6_000)
                        return sel
                    except Exception:
                        continue
            except Exception as exc:
                logger.debug(f"[Generic] selector {sel!r} failed: {exc}")
        return None

    async def submit(self, page: Page) -> bool:
        learned = get_learned_fixes("generic").get("submit")
        selectors = learned + [s for s in _SUBMIT_SELECTORS if s not in learned]

        async def try_selectors() -> bool:
            for ctx in self._search_contexts(page):
                sel = await self._click_first_actionable(ctx, selectors)
                if sel:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=12_000)
                    except Exception:
                        pass
                    await self.human_delay(0.8, 1.6)
                    get_learned_fixes("generic").add("submit", sel)
                    logger.info(f"[Generic] submit via {sel!r}")
                    return True
            return False

        # Attempt 1: Try all selectors as is
        if await try_selectors():
            return True

        # Attempt 2: Scroll to bottom, then retry all selectors
        logger.info("[Generic] Submit buttons not found. Scrolling to bottom to retry...")
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self.human_delay(1.0, 1.5)
        except Exception as exc:
            logger.debug(f"[Generic] failed to scroll to bottom before retry: {exc}")

        if await try_selectors():
            return True

        # Attempt 3: multi-step form — advance via Next/Continue (bounded) and
        # retry the submit sweep on each new step.
        for _ in range(4):
            advanced = None
            for ctx in self._search_contexts(page):
                advanced = await self._click_first_actionable(ctx, _NEXT_STEP_SELECTORS)
                if advanced:
                    break
            if not advanced:
                break
            logger.info(f"[Generic] advanced multi-step form via {advanced!r}; retrying submit")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            await self.human_delay(1.0, 1.8)
            if await try_selectors():
                return True

        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        try:
            content = (await page.content()).lower()
        except Exception:
            return False, None
        for pattern in _SUCCESS_PATTERNS:
            if pattern in content:
                return True, pattern
        return False, None
