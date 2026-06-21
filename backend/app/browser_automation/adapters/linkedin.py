"""LinkedIn Easy Apply adapter.

LinkedIn requires an authenticated session — the Easy Apply modal will not
open for logged-out users. We rely on the BrowserContextManager's Redis
session restore: cookies for `session:<candidate_id>:linkedin` must already
be present (you can seed them by running a manual login once and letting
context_manager.save_session() persist them).

The Easy Apply flow is a modal that walks 1-5 steps. Each step has a
"Continue to next step" or "Review your application" button. The final step
shows "Submit application". We loop with a bounded step cap to avoid
infinite loops if a step refuses to validate.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple

from playwright.async_api import Page

from .base import BasePlatformAdapter
from ..agent import get_learned_fixes
from ..forms import detect_form, fill_form, upload_file

logger = logging.getLogger(__name__)


_MODAL_SEL = ".jobs-easy-apply-modal, [aria-labelledby*='easy-apply']"

_EASY_APPLY_SELECTORS = [
    "button.jobs-apply-button:has-text('Easy Apply')",
    ".jobs-apply-button",
    "[data-job-id] .artdeco-button--primary",
    "button:has-text('Easy Apply')",
]

_NEXT_SELECTORS = [
    "button[aria-label='Continue to next step']",
    "button[aria-label='Review your application']",
    ".artdeco-button--primary:has-text('Next')",
    ".artdeco-button--primary:has-text('Continue')",
    ".artdeco-button--primary:has-text('Review')",
]

_SUBMIT_SELECTORS = [
    "button[aria-label='Submit application']",
    "button:has-text('Submit application')",
    ".artdeco-button--primary:has-text('Submit')",
]

_AUTH_WALL_HINTS = (
    "sign in to view",
    "join now to view",
    "join now to continue",
    "sign in to linkedin",
)

_MAX_STEPS = 6


class LinkedInEasyApplyAdapter(BasePlatformAdapter):
    platform_name = "linkedin"
    container_selector = ".jobs-easy-apply-modal"

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.warning(f"[LinkedIn] navigate timeout: {exc}")
        await self.human_delay(1.0, 2.0)

        # Auth-wall check
        try:
            content = (await page.content()).lower()
        except Exception:
            content = ""
        if any(h in content[:6_000] for h in _AUTH_WALL_HINTS):
            raise RuntimeError(
                "BLOCKED: LinkedIn requires an authenticated session. "
                "Seed cookies via context_manager.save_session() for this candidate."
            )

        # Open Easy Apply modal
        learned = get_learned_fixes("linkedin").get("easy_apply_button")
        opened = False
        for sel in learned + [s for s in _EASY_APPLY_SELECTORS if s not in learned]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=6_000)
                    get_learned_fixes("linkedin").add("easy_apply_button", sel)
                    logger.info(f"[LinkedIn] Easy Apply opened via {sel!r}")
                    opened = True
                    break
            except Exception as exc:
                logger.debug(f"[LinkedIn] Easy Apply selector {sel!r} failed: {exc}")
        if not opened:
            raise RuntimeError(
                "BLOCKED: Easy Apply button not found. Job may not support Easy Apply, "
                "session may be unauthenticated, or LinkedIn DOM has shifted."
            )

        try:
            await page.locator(_MODAL_SEL).first.wait_for(state="visible", timeout=8_000)
        except Exception:
            logger.warning("[LinkedIn] Easy Apply modal did not appear within 8s")

        await self.human_delay(0.5, 1.0)

    async def detect_application_type(self, page: Page) -> str:
        return "EASY_APPLY"

    async def fill_application(
        self,
        page: Page,
        profile: dict,
        resume_path: str,
        cover_letter_path: Optional[str],
        screening_answers: Optional[dict],
        pre_detected_form=None, candidate_id=None,
    ) -> bool:
        ok = True
        for step in range(1, _MAX_STEPS + 1):
            modal = await page.query_selector(_MODAL_SEL)
            if not modal:
                logger.info(f"[LinkedIn] no modal at step {step} — exiting loop")
                break

            try:
                form = pre_detected_form if step == 1 else None
                form = form or await detect_form(page, container_selector=self.container_selector)
            except Exception as exc:
                logger.warning(f"[LinkedIn] detect_form on step {step} failed: {exc}")
                break

            if form.fields:
                step_ok = await fill_form(page, form, profile, screening_answers, candidate_id=candidate_id)
                ok = ok and step_ok
                for field in form.fields:
                    if field.field_type != "file":
                        continue
                    lbl = (field.label or "").lower()
                    try:
                        if cover_letter_path and "cover" in lbl:
                            await upload_file(page, field.selector, cover_letter_path)
                        elif "resume" in lbl or "cv" in lbl:
                            await upload_file(page, field.selector, resume_path)
                    except Exception as exc:
                        logger.warning(f"[LinkedIn] upload failed for {field.label!r}: {exc}")

            # Try Next; if Next is missing assume we're on the Submit step
            advanced = await self._click_one(page, "next_step", _NEXT_SELECTORS)
            if not advanced:
                logger.info(f"[LinkedIn] step {step}: no Next — submit step reached")
                break
            await asyncio.sleep(1.2)
            pre_detected_form = None

        return ok

    async def submit(self, page: Page) -> bool:
        return await self._click_one(page, "submit", _SUBMIT_SELECTORS)

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        try:
            content = await page.content()
        except Exception:
            return False, None
        for pattern in (
            "Your application was sent",
            "Application sent",
            "Done",
        ):
            if pattern in content:
                return True, pattern
        return False, None

    async def _click_one(self, page: Page, channel: str, candidates: list) -> bool:
        learned = get_learned_fixes("linkedin").get(channel)
        for sel in learned + [s for s in candidates if s not in learned]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=6_000)
                    get_learned_fixes("linkedin").add(channel, sel)
                    logger.info(f"[LinkedIn] {channel} via {sel!r}")
                    return True
            except Exception as exc:
                logger.debug(f"[LinkedIn] {channel} selector {sel!r} failed: {exc}")
        return False
