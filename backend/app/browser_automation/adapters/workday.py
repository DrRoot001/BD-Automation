"""Workday (myworkdayjobs.com) adapter.

Workday is the hardest mainstream ATS to automate. Pattern::

    https://<company>.wd<N>.myworkdayjobs.com/en-US/<board>/job/<location>/<title>_<id>

Key challenges this adapter handles:

  1. **Apply gate** — the job page shows "Apply" -> "Apply Manually" / "Use My
     Last Application" / "Apply with LinkedIn". We always pick "Apply Manually".

  2. **Account wall** — Workday requires an account (sign-in or create) before
     showing the application form. If WORKDAY_USERNAME / WORKDAY_PASSWORD env
     vars are set we sign in; otherwise we surface a clear BLOCKED error so
     the operator knows credentials are required.

  3. **Multi-step questionnaire** — usually 4-6 numbered steps (My Information,
     My Experience, Application Questions, Voluntary Disclosures, Self-Identify,
     Review). We loop through them clicking "Save and Continue" until we hit
     "Submit".

  4. **Custom widgets** — Workday's selects, date pickers, and file uploaders
     are bespoke. The detector already flags `custom_widget=True` for selects
     it can't drive natively; the filler handles them via click-then-type-then-
     enter fallback.

Because Workday's DOM changes every few months, this adapter leans heavily on
learned_fixes + the vision page-agent rather than hardcoded selectors.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Tuple

from playwright.async_api import Page

from .base import BasePlatformAdapter
from ..agent import get_learned_fixes
from ..forms import detect_form, fill_form, upload_file

logger = logging.getLogger(__name__)

_APPLY_SELECTORS = [
    "[data-automation-id='applyToJobButton']",
    "[data-automation-id='adventureButton']",
    "button:has-text('Apply')",
    "a:has-text('Apply')",
]

_APPLY_MANUALLY_SELECTORS = [
    "[data-automation-id='applyManually']",
    "a:has-text('Apply Manually')",
    "button:has-text('Apply Manually')",
    "div[role='button']:has-text('Apply Manually')",
]

_SIGNIN_LINK_SELECTORS = [
    "[data-automation-id='signInLink']",
    "button:has-text('Sign In')",
    "a:has-text('Sign In')",
]

_NEXT_STEP_SELECTORS = [
    "[data-automation-id='pageFooterNextButton']",
    "[data-automation-id='bottom-navigation-next-button']",
    "button:has-text('Save and Continue')",
    "button:has-text('Continue')",
    "button:has-text('Next')",
]

_SUBMIT_SELECTORS = [
    "[data-automation-id='pageFooterSubmitButton']",
    "[data-automation-id='bottom-navigation-submit-button']",
    "button:has-text('Submit')",
    "button:has-text('Submit Application')",
]

_SUCCESS_PATTERNS = (
    "you have submitted your application",
    "thank you for your interest",
    "application submitted",
    "successfully submitted",
    "we have received your application",
)

_MAX_STEPS = 8  # safety cap on the multi-step loop


class WorkdayAdapter(BasePlatformAdapter):
    platform_name = "workday"
    container_selector = None

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.warning(f"[Workday] initial navigate timeout: {exc}")
        await self.human_delay(1.0, 2.0)

        # Click Apply
        if not await self._click_one_of(page, "apply_button", _APPLY_SELECTORS):
            raise RuntimeError("BLOCKED: Workday Apply button not found — job may be closed or DOM changed")

        await self.human_delay(0.6, 1.2)

        # Choose "Apply Manually" if a chooser appears
        try:
            await page.locator(", ".join(_APPLY_MANUALLY_SELECTORS)).first.wait_for(
                state="visible", timeout=4_000
            )
            await self._click_one_of(page, "apply_manually", _APPLY_MANUALLY_SELECTORS)
        except Exception:
            logger.debug("[Workday] no Apply Manually chooser appeared")

        await self.human_delay(0.6, 1.2)

        # Handle account wall
        await self._sign_in_if_required(page)

        # Wait for the first form step to render
        try:
            await page.locator("input, textarea, select").first.wait_for(state="attached", timeout=15_000)
        except Exception:
            logger.warning("[Workday] form inputs did not attach within 15s; proceeding anyway")

    async def detect_application_type(self, page: Page) -> str:
        return "EXTERNAL_FORM"

    async def fill_application(
        self,
        page: Page,
        profile: dict,
        resume_path: str,
        cover_letter_path: Optional[str],
        screening_answers: Optional[dict],
        pre_detected_form=None, candidate_id=None,
    ) -> bool:
        """Walk the multi-step Workday form. On each step: detect form, fill it,
        upload files, click Save and Continue. Stop when no Next button found
        — at which point submit() takes over."""
        ok = True
        for step in range(1, _MAX_STEPS + 1):
            logger.info(f"[Workday] === step {step} ===")
            try:
                form = pre_detected_form if step == 1 else None
                form = form or await detect_form(page, container_selector=self.container_selector)
            except Exception as exc:
                logger.warning(f"[Workday] form detect on step {step} failed: {exc}")
                break

            if not form.fields:
                logger.info(f"[Workday] step {step} has no fields — likely the review page")
                break

            step_ok = await fill_form(page, form, profile, screening_answers, candidate_id=candidate_id)
            ok = ok and step_ok

            # Upload files this step
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
                    logger.warning(f"[Workday] upload failed for {field.label!r}: {exc}")

            # Try to advance — if no Next found, we're on the review/submit page
            advanced = await self._click_one_of(page, "next_step", _NEXT_STEP_SELECTORS)
            if not advanced:
                logger.info(f"[Workday] step {step}: no Next button — assume review page reached")
                break
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            await self.human_delay(0.8, 1.6)
            pre_detected_form = None

        return ok

    async def submit(self, page: Page) -> bool:
        learned = get_learned_fixes("workday").get("submit")
        for sel in learned + [s for s in _SUBMIT_SELECTORS if s not in learned]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=8_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    await self.human_delay(1.0, 2.0)
                    get_learned_fixes("workday").add("submit", sel)
                    logger.info(f"[Workday] submit via {sel!r}")
                    return True
            except Exception as exc:
                logger.debug(f"[Workday] submit selector {sel!r} failed: {exc}")
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

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _click_one_of(self, page: Page, channel: str, candidates: list) -> bool:
        """Try selectors from learned_fixes first, then the hardcoded list.
        Persists the winner to learned_fixes[channel]."""
        learned = get_learned_fixes("workday").get(channel)
        for sel in learned + [s for s in candidates if s not in learned]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=5_000)
                    get_learned_fixes("workday").add(channel, sel)
                    logger.info(f"[Workday] {channel} via {sel!r}")
                    return True
            except Exception as exc:
                logger.debug(f"[Workday] {channel} selector {sel!r} failed: {exc}")
        return False

    async def _sign_in_if_required(self, page: Page) -> None:
        """If Workday is showing the account wall, sign in with env credentials
        or raise BLOCKED. Workday requires an account before form access.
        """
        try:
            visible_signin = await page.locator(", ".join(_SIGNIN_LINK_SELECTORS)).first.is_visible(timeout=3_000)
        except Exception:
            visible_signin = False
        if not visible_signin:
            return

        username = os.getenv("WORKDAY_USERNAME", "").strip()
        password = os.getenv("WORKDAY_PASSWORD", "").strip()
        if not username or not password:
            raise RuntimeError(
                "BLOCKED: Workday requires an account; set WORKDAY_USERNAME and "
                "WORKDAY_PASSWORD in .env (one set of credentials per candidate)"
            )

        logger.info("[Workday] signing in with stored credentials")
        await self._click_one_of(page, "signin_link", _SIGNIN_LINK_SELECTORS)
        await self.human_delay(0.5, 1.0)
        try:
            await page.locator("[data-automation-id='email']").fill(username, timeout=5_000)
            await page.locator("[data-automation-id='password']").fill(password, timeout=5_000)
            await page.locator("[data-automation-id='click_filter']").click(timeout=5_000)
            await page.wait_for_load_state("domcontentloaded", timeout=12_000)
        except Exception as exc:
            raise RuntimeError(f"BLOCKED: Workday sign-in failed: {exc}")
        await asyncio.sleep(1.5)
