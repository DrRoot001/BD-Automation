"""Lever (jobs.lever.co) adapter.

Lever's hosted boards follow this URL pattern::

    https://jobs.lever.co/<company>/<job-uuid>          ← job description page
    https://jobs.lever.co/<company>/<job-uuid>/apply    ← application form page

The job-description page has a "Apply for this job" link that routes to /apply.
The form itself is plain HTML, no iframe, container `#application-form` with
classic field naming. File inputs are standard <input type=file>.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from playwright.async_api import Page

from .base import BasePlatformAdapter
from ..agent import get_learned_fixes
from ..forms import detect_form, fill_form, upload_file

logger = logging.getLogger(__name__)


_APPLY_SELECTORS = [
    "a.postings-btn[href*='/apply']",
    "a.template-btn-submit[href*='/apply']",
    "a[href$='/apply']",
    "a:has-text('Apply for this job')",
    "a:has-text('Apply')",
    "button:has-text('Apply for this job')",
]

_SUBMIT_SELECTORS = [
    "button[data-qa='btn-submit']",
    ".template-btn-submit",
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Submit application')",
    "button:has-text('Submit')",
]

_SUCCESS_PATTERNS = (
    "thanks for applying",
    "thank you for applying",
    "application submitted",
    "we've received your application",
    "your application has been received",
)


class LeverAdapter(BasePlatformAdapter):
    platform_name = "lever"
    container_selector = "#application-form"

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        # If the URL is a job-description page, append /apply directly — saves an Apply click.
        target = job_url.rstrip("/")
        if not target.endswith("/apply"):
            target = target + "/apply"

        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=25_000)
        except Exception as exc:
            logger.warning(f"[Lever] direct /apply navigation failed: {exc}; retrying original URL")
            try:
                await page.goto(job_url, wait_until="domcontentloaded", timeout=25_000)
            except Exception:
                pass

        # If we're still on the listing (no form inputs found), click Apply.
        try:
            await page.locator("#application-form, input[name='name'], input[name='email']").first.wait_for(
                state="attached", timeout=3_000
            )
        except Exception:
            logger.info("[Lever] no form on page yet; trying Apply button")
            learned = get_learned_fixes("lever").get("apply_button")
            for sel in learned + _APPLY_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.scroll_into_view_if_needed()
                        await btn.click(timeout=5_000)
                        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                        get_learned_fixes("lever").add("apply_button", sel)
                        break
                except Exception:
                    continue

        await self.human_delay(0.5, 1.5)

    async def detect_application_type(self, page: Page) -> str:
        return "EXTERNAL_FORM"

    async def fill_application(
        self,
        page: Page,
        profile: dict,
        resume_path: str,
        cover_letter_path: Optional[str],
        screening_answers: Optional[dict],
        pre_detected_form=None,
    ) -> bool:
        form = pre_detected_form or await detect_form(page, container_selector=self.container_selector)
        fill_success = await fill_form(page, form, profile, screening_answers)

        for field in form.fields:
            if field.field_type != "file":
                continue
            lbl = (field.label or "").lower()
            if cover_letter_path and "cover" in lbl:
                await upload_file(page, field.selector, cover_letter_path)
            elif "resume" in lbl or "cv" in lbl or not any(k in lbl for k in ("cover", "other")):
                await upload_file(page, field.selector, resume_path)

        return fill_success

    async def submit(self, page: Page) -> bool:
        learned = get_learned_fixes("lever").get("submit")
        for sel in learned + [s for s in _SUBMIT_SELECTORS if s not in learned]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=6_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=12_000)
                    except Exception:
                        await page.wait_for_load_state("domcontentloaded", timeout=8_000)
                    await self.human_delay(1.0, 2.0)
                    get_learned_fixes("lever").add("submit", sel)
                    logger.info(f"[Lever] submit via {sel!r}")
                    return True
            except Exception as exc:
                logger.debug(f"[Lever] submit selector {sel!r} failed: {exc}")
        logger.error("[Lever] no submit selector matched")
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
