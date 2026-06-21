"""Ashby (jobs.ashbyhq.com) adapter.

Ashby is React-heavy with custom select widgets. URL pattern::

    https://jobs.ashbyhq.com/<company>/<job-id>
    https://jobs.ashbyhq.com/<company>/<job-id>/application

Some companies embed Ashby via an iframe (`#ashby_embed_iframe`) on their own
careers page; we detect that the same way Greenhouse does.

The "Apply" CTA on the job-description page navigates to /application. The
form fields use Ashby's custom React widgets (custom dropdowns, file pickers
that POST to an upload endpoint then attach the URL to a hidden input).
The detector + filler already handle `custom_widget=True` selects.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from playwright.async_api import Frame, Page

from .base import BasePlatformAdapter
from ..agent import get_learned_fixes
from ..forms import detect_form, fill_form, upload_file

logger = logging.getLogger(__name__)

_IFRAME_SEL = "#ashby_embed_iframe, iframe[src*='ashbyhq']"

_APPLY_SELECTORS = [
    "a[href*='/application']",
    "a:has-text('Apply for this Job')",
    "a:has-text('Apply for this job')",
    "button:has-text('Apply for this Job')",
    "button:has-text('Apply Now')",
    "a:has-text('Apply')",
    ".ashby-job-posting-apply-button",
]

_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "button:has-text('Submit Application')",
    "button:has-text('Submit application')",
    "button:has-text('Submit')",
    ".ashby-application-submit-button",
]

_SUCCESS_PATTERNS = (
    "application received",
    "you've applied",
    "you have applied",
    "thank you for applying",
    "we've received your application",
)


class AshbyAdapter(BasePlatformAdapter):
    platform_name = "ashby"
    container_selector = None  # Ashby form is full page; no stable wrapper id

    def __init__(self):
        self._iframe_mode: bool = False
        self._frame: Optional[Frame] = None

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        # If on the job description page, prefer /application directly
        target = job_url.rstrip("/")
        if "/application" not in target:
            target = target + "/application"
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=25_000)
        except Exception as exc:
            logger.warning(f"[Ashby] /application navigate failed ({exc}); retrying base URL")
            try:
                await page.goto(job_url, wait_until="domcontentloaded", timeout=25_000)
            except Exception:
                pass

        # Detect iframe embed
        self._iframe_mode = False
        self._frame = None
        try:
            await page.wait_for_selector(_IFRAME_SEL, timeout=4_000)
            for fr in page.frames:
                if "ashbyhq" in (fr.url or ""):
                    self._frame = fr
                    self._iframe_mode = True
                    logger.info(f"[Ashby] iframe mode (frame={fr.url})")
                    try:
                        await fr.wait_for_load_state("domcontentloaded", timeout=10_000)
                    except Exception:
                        pass
                    break
        except Exception:
            pass

        target_ctx = self._frame if self._iframe_mode else page

        # If still no inputs visible, click Apply
        try:
            await target_ctx.locator("input, textarea, select").first.wait_for(state="attached", timeout=3_000)
        except Exception:
            learned = get_learned_fixes("ashby").get("apply_button")
            for sel in learned + _APPLY_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.scroll_into_view_if_needed()
                        await btn.click(timeout=5_000)
                        await page.wait_for_load_state("domcontentloaded", timeout=12_000)
                        get_learned_fixes("ashby").add("apply_button", sel)
                        logger.info(f"[Ashby] Apply clicked via {sel!r}")
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
        pre_detected_form=None, candidate_id=None,
    ) -> bool:
        ctx = self._frame if self._iframe_mode else page
        form = pre_detected_form or await detect_form(ctx, container_selector=self.container_selector)
        fill_success = await fill_form(ctx, form, profile, screening_answers, candidate_id=candidate_id)

        for field in form.fields:
            if field.field_type != "file":
                continue
            lbl = (field.label or "").lower()
            if cover_letter_path and "cover" in lbl:
                await upload_file(ctx, field.selector, cover_letter_path)
            elif "resume" in lbl or "cv" in lbl or not any(k in lbl for k in ("cover", "other")):
                await upload_file(ctx, field.selector, resume_path)

        return fill_success

    async def submit(self, page: Page) -> bool:
        ctx = self._frame if self._iframe_mode else page
        learned = get_learned_fixes("ashby").get("submit")
        for sel in learned + [s for s in _SUBMIT_SELECTORS if s not in learned]:
            try:
                btn = ctx.locator(sel).first
                if await btn.count() > 0:
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=6_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=12_000)
                    except Exception:
                        pass
                    await self.human_delay(1.0, 2.0)
                    get_learned_fixes("ashby").add("submit", sel)
                    logger.info(f"[Ashby] submit via {sel!r}")
                    return True
            except Exception as exc:
                logger.debug(f"[Ashby] submit selector {sel!r} failed: {exc}")
        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        for ctx in ([self._frame, page] if self._iframe_mode else [page]):
            if ctx is None:
                continue
            try:
                content = (await ctx.content()).lower()
            except Exception:
                continue
            for pattern in _SUCCESS_PATTERNS:
                if pattern in content:
                    return True, pattern
        return False, None
