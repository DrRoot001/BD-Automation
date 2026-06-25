"""iCIMS adapter — vision-driven flow.

iCIMS portals host applications under several URL shapes:
  - careers.icims.com/careers-home/jobs/<id>
  - <tenant>.icims.com/jobs/<id>/...
  - globalcareers-<n>.icims.com/jobs/<id>/...

Flow observed on the reference posting (careers.icims.com/careers-home/jobs/6452):
  1. Job description page          → click "Apply for this job"
  2. Email-consent screen          → enter email, tick consent checkbox, click Next
  3. Candidate Profile (step 1/2)  → upload resume, create login, fill name /
                                    contact / address / source → "Submit Profile"
  4. Candidate Questions (step 2/2) → screening Q&A → final Submit

The adapter does NOT script any of those steps — the AgentLoop drives every
click and field using vision + DOM + the iCIMS-specific hints in
``adapters/hints.py``. The adapter's job is limited to:

  - Loading the job URL with bot-wall detection and cookie-banner dismissal.
  - Reporting form context so the executor's vision oversight stays in sync.
  - Providing fallback submit / success-detection if the AgentLoop ever bails
    out on the deterministic pipeline (rare for iCIMS).

iCIMS has no Greenhouse-style same-page iframe to detect; the application form
lives in the top-level frame after the Apply click (sometimes after a host
swap to ``globalcareers-customerN.icims.com``). We treat the page as a single
non-iframe context throughout.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple

from playwright.async_api import Page

from .base import BasePlatformAdapter

logger = logging.getLogger(__name__)

_NAV_TIMEOUT_MS = 25_000
_FIELD_TIMEOUT_MS = 6_000

# Apply-button selectors observed across iCIMS tenants. The vision agent will
# also pick these up from hints.py; keeping a hard list here lets the
# deterministic fallback work without the LLM.
_APPLY_SELECTORS = (
    "a#applyButton",
    "a.iCIMS_Anchor_ApplyOnline",
    "a:has-text('Apply for this job')",
    "a:has-text('Apply for this Job')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply for this job')",
    "button:has-text('Apply Now')",
    "input.iCIMS_PrimaryButton[value*='Apply']",
)

# Final-submit selectors for the two-step candidate flow.
_SUBMIT_SELECTORS = (
    "input#cp_form_submit_i",
    "input.iCIMS_PrimaryButton[type='submit']",
    "input.iCIMS_PrimaryButton[value='Submit Profile']",
    "input.iCIMS_PrimaryButton[value='Submit']",
    "button.iCIMS_PrimaryButton",
    "button[type='submit']:has-text('Submit')",
)

# Plain-text confirmation phrases iCIMS shows after a successful submission.
_SUCCESS_PATTERNS = (
    "thank you for applying",
    "your application has been submitted",
    "your application was submitted",
    "application received",
    "thanks for your interest",
)

# Bot-wall / blocked-page indicators (CloudFront / WAF / iCIMS rate limit).
_BLOCK_PATTERNS = (
    "request could not be satisfied",
    "request blocked",
    "access denied",
    "too many requests",
)


class ICIMSAdapter(BasePlatformAdapter):
    platform_name = "icims"
    container_selector = ".iCIMS_MainWrapper"

    def __init__(self) -> None:
        # Mirror the attribute names other adapters expose so the executor's
        # ``getattr(adapter, "_frame_locator", ...)`` reads return None cleanly.
        self._iframe_mode: bool = False
        self._frame_locator = None
        self._frame = None

    # ──────────────────────────────────────────────────────────────────────
    # Navigation
    # ──────────────────────────────────────────────────────────────────────

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        logger.info(f"[iCIMS] Navigating to {job_url!r}")
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        except Exception as exc:
            logger.warning(f"[iCIMS] Initial goto soft-failed ({exc}); settling page")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8_000)
            except Exception:
                pass

        # Wait for either the job header or the embedded application form to
        # exist — whichever shows first tells us whether we need an Apply click.
        try:
            await page.wait_for_selector(
                "h1.iCIMS_Header, .iCIMS_MainWrapper, .iCIMS_CandidatePage, "
                "#iCIMS_ApplyOnlineLink, input.iCIMS_PrimaryButton",
                timeout=_FIELD_TIMEOUT_MS,
            )
        except Exception:
            logger.warning("[iCIMS] No iCIMS wrapper detected within timeout — page may not be iCIMS")

        # Bot-wall check.
        try:
            title = (await page.title()).lower()
        except Exception:
            title = ""
        content_snippet = ""
        try:
            content_snippet = (await page.content())[:1200].lower()
        except Exception:
            pass
        if any(p in title or p in content_snippet for p in _BLOCK_PATTERNS):
            logger.error(f"[iCIMS] Bot wall detected at {page.url!r} — title={title!r}")
            raise RuntimeError(f"BLOCKED: iCIMS bot wall at {page.url}")

        # Dismiss common cookie banners (OneTrust / iCIMS native consent).
        for sel in (
            "button#onetrust-accept-btn-handler",
            "button#onetrust-reject-all-handler",
            "button.osano-cm-accept",
            "button:has-text('Accept All')",
            "button:has-text('Accept')",
        ):
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=2_000)
                    logger.info(f"[iCIMS] Dismissed cookie banner via {sel!r}")
                    await asyncio.sleep(0.4)
                    break
            except Exception:
                continue

        logger.info(f"[iCIMS] Landed on {page.url!r} (title={title!r})")
        await self.human_delay(0.5, 1.5)

    async def detect_application_type(self, page: Page) -> str:
        return "EXTERNAL_FORM"

    async def refresh_frame(self, page: Page) -> None:
        # iCIMS has no persistent iframe for us to track — no-op.
        return None

    # ──────────────────────────────────────────────────────────────────────
    # Fill / submit — fallback only. The AgentLoop normally drives this end
    # to end via vision + hints.
    # ──────────────────────────────────────────────────────────────────────

    async def fill_application(
        self,
        page: Page,
        profile: dict,
        resume_path: str,
        cover_letter_path: Optional[str],
        screening_answers: Optional[dict],
        pre_detected_form=None,
        candidate_id: Optional[str] = None,
    ) -> bool:
        from ..forms import detect_form, fill_form

        form = pre_detected_form or await detect_form(page, container_selector=self.container_selector)
        return await fill_form(page, form, profile, screening_answers, candidate_id=candidate_id)

    async def submit(self, page: Page) -> bool:
        for sel in _SUBMIT_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0:
                    continue
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=_FIELD_TIMEOUT_MS)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                await self.human_delay(1.0, 2.0)
                logger.info(f"[iCIMS] Submitted via {sel!r}")
                return True
            except Exception as exc:
                logger.debug(f"[iCIMS] Submit selector {sel!r} failed: {exc}")
        logger.error("[iCIMS] No submit button matched")
        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        try:
            content = (await page.content()).lower()
        except Exception:
            return (False, None)
        for pattern in _SUCCESS_PATTERNS:
            if pattern in content:
                logger.info(f"[iCIMS] Submission verified via pattern {pattern!r}")
                return (True, pattern)
        return (False, None)
