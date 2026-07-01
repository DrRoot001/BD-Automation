"""Built In (builtin.com) adapter.

Built In hosts a native ATS at ``builtin.com/apply/...``. Unlike a job-board
that redirects to a third-party ATS, Built In runs its own application flow:

    https://builtin.com/apply/job/{job-id}
    https://builtin.com/apply/...             ← single-URL apply flow

Flow (per operator spec v1.0):
    Job listing → EASY APPLY → apply form → resume upload → auto-parse →
    personal info → work experience → education (modal) → compliance
    questionnaires (sponsorship / government / conflict-of-interest) →
    review → submit → success page.

Architecture note (matches operator standing rule "AI master / Playwright
slave"): this adapter does NOT hand-script the multi-step wizard. It
navigates to the apply URL, hands the page to the AgentLoop, and provides
Built In quirks via the hints layer. Compliance-question policy answers
(sponsorship / government official / conflict-of-interest) come through
the shared ``_policy_fields`` machinery in ``agent/loop.py``.
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
    "button:has-text('Easy Apply')",
    "a:has-text('Easy Apply')",
    "button:has-text('Apply Now')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply')",
    "a:has-text('Apply')",
    "[data-testid*='apply']",
    "[aria-label*='Easy Apply']",
    "[aria-label*='Apply']",
]

_SUBMIT_SELECTORS = [
    "button:has-text('Submit Application')",
    "button:has-text('Submit application')",
    "button:has-text('Send Application')",
    "button:has-text('Complete Application')",
    "button:has-text('Submit')",
    "button[type='submit']",
    "input[type='submit']",
    "[data-testid*='submit']",
    "[aria-label*='Submit']",
]

# Substrings to look for in the confirmation page body / title.
_SUCCESS_PATTERNS = (
    "application submitted",
    "application received",
    "application complete",
    "thank you for applying",
    "thanks for applying",
    "we've received your application",
    "we have received your application",
    "your application has been received",
    "your application was sent",
)

# URL fragments a successful submit tends to land on.
_SUCCESS_URL_PATTERNS = (
    "/success",
    "/submitted",
    "/thank-you",
    "/thankyou",
    "/thanks",
    "/complete",
    "/confirmation",
)


class BuiltInAdapter(BasePlatformAdapter):
    platform_name = "builtin"
    # Built In's form doesn't have a single canonical container id — the
    # AgentLoop will find fields via role/label selectors per the spec's
    # "Priority 1: getByLabel()" recommendation.
    container_selector = None

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        """Land on the Built In apply page. The URL pattern
        ``builtin.com/apply/...`` already IS the application page for this
        platform — no separate "Apply" click is typically needed. If the URL
        happens to be a job-description page instead, try clicking Easy Apply.
        """
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=25_000)
        except Exception as exc:
            logger.warning(f"[BuiltIn] initial navigation failed: {exc}")

        # If we're not already on an apply/form page, hunt for the Easy Apply
        # button. Cheap check: look for any recognizable form-input first.
        try:
            await page.locator(
                "input[type='file'], input[name*='email'], input[name*='name']"
            ).first.wait_for(state="attached", timeout=3_000)
        except Exception:
            logger.info("[BuiltIn] no form fields visible yet; trying Easy Apply button")
            learned = get_learned_fixes("builtin").get("apply_button")
            for sel in learned + _APPLY_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.scroll_into_view_if_needed()
                        await btn.click(timeout=5_000)
                        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                        get_learned_fixes("builtin").add("apply_button", sel)
                        logger.info(f"[BuiltIn] Easy Apply clicked via {sel!r}")
                        break
                except Exception:
                    continue

        await self.human_delay(0.5, 1.5)

    async def detect_application_type(self, page: Page) -> str:
        # Built In runs a native form on their own domain — treat like any
        # other native-form ATS (Lever/Ashby/Greenhouse). No LinkedIn-style
        # OAuth / EasyApply-modal branch.
        return "EXTERNAL_FORM"

    async def fill_application(
        self,
        page: Page,
        profile: dict,
        resume_path: str,
        cover_letter_path: Optional[str],
        screening_answers: Optional[dict],
        pre_detected_form=None,
        candidate_id=None,
    ) -> bool:
        # Deterministic pre-pass mirrors the Lever/Ashby adapters: hand the
        # detected form to fill_form + attach resume/cover-letter files.
        # AgentLoop then handles the multi-step (personal → work → education
        # modal → compliance) portion using the hints and policy answers.
        form = pre_detected_form or await detect_form(
            page, container_selector=self.container_selector
        )
        fill_success = await fill_form(
            page, form, profile, screening_answers, candidate_id=candidate_id
        )

        for field in form.fields:
            if field.field_type != "file":
                continue
            lbl = (field.label or "").lower()
            if cover_letter_path and "cover" in lbl:
                await upload_file(page, field.selector, cover_letter_path)
            elif "resume" in lbl or "cv" in lbl or not any(
                k in lbl for k in ("cover", "other")
            ):
                await upload_file(page, field.selector, resume_path)

        return fill_success

    async def submit(self, page: Page) -> bool:
        learned = get_learned_fixes("builtin").get("submit")
        candidates = learned + [s for s in _SUBMIT_SELECTORS if s not in learned]
        for sel in candidates:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=6_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=12_000)
                    except Exception:
                        await page.wait_for_load_state("domcontentloaded", timeout=8_000)
                    await self.human_delay(1.0, 2.0)
                    get_learned_fixes("builtin").add("submit", sel)
                    logger.info(f"[BuiltIn] submit via {sel!r}")
                    return True
            except Exception as exc:
                logger.debug(f"[BuiltIn] submit selector {sel!r} failed: {exc}")
        logger.error("[BuiltIn] no submit selector matched")
        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        # Spec explicitly warns "do not depend solely on URL" — check both
        # URL patterns AND body content. Body content match wins first
        # because it survives redirect-suppression.
        try:
            content = (await page.content()).lower()
        except Exception:
            content = ""
        for pattern in _SUCCESS_PATTERNS:
            if pattern in content:
                return True, pattern

        try:
            url = (page.url or "").lower()
        except Exception:
            url = ""
        for pattern in _SUCCESS_URL_PATTERNS:
            if pattern in url:
                return True, f"url:{pattern}"

        return False, None
