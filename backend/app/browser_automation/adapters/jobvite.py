"""Jobvite (jobs.jobvite.com) adapter.

URL patterns::

    https://jobs.jobvite.com/<company>/job/<jobId>          (posting)
    https://jobs.jobvite.com/<company>/job/<jobId>/apply    (application form)

The posting page's Apply CTA is a plain ``<a>`` to the same path + ``/apply``,
so navigation is deterministic: rewrite the path, no vision click required.

The application is a single page in the LIGHT DOM (no shadow roots), but every
field id/name is a per-tenant random token (``jv-field-y9GvXfw1`` /
``input-y9GvXfw1``) — labels are the only stable anchors, which is exactly the
AgentLoop's strength. This adapter therefore follows "AI master / Playwright
slave": it navigates and exposes portal knowledge via hints
(adapters/hints.py "jobvite"), deferring form-fill to the vision AgentLoop.

Form invariants verified live (2026-07-11, Uplight tenant):
  - Resume section usually REQUIRED — 'Select' button + file input, or the
    'Type or paste your Resume here' textarea as fallback;
  - required contact fields: First Name, Last Name, Email, Country (select),
    City; plus per-tenant screening selects (work authorization, sponsorship);
  - an embedded reCAPTCHA v2 (``#g-recaptcha-response``) gates the submit;
  - flow: 'Next →' steps through sections, final button is 'Send Application';
  - a 'LinkedIn' import button must never be clicked (manual-fill-only policy).
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple
from urllib.parse import urlparse, urlunparse

from playwright.async_api import Page

from .base import BasePlatformAdapter
from ..agent import get_learned_fixes
from ..forms import detect_form, fill_form, upload_file

logger = logging.getLogger(__name__)

_APPLY_SELECTORS = [
    "a[href*='/apply']",
    "a:has-text('Apply')",
    "button:has-text('Apply')",
]

_SUBMIT_SELECTORS = [
    "button:has-text('Send Application')",
    "button[type='submit']",
]

# Specific phrases only — never a bare "thank you".
_SUCCESS_PATTERNS = (
    "thank you for applying",
    "application has been sent",
    "application has been received",
    "successfully submitted",
    "application submitted",
)

_ALREADY_APPLIED_PATTERNS = (
    "already applied",
    "you have already applied",
    "already submitted an application",
)


class JobviteAdapter(BasePlatformAdapter):
    platform_name = "jobvite"
    container_selector = None

    def __init__(self):
        self._iframe_mode: bool = False
        self._frame = None
        self._resolved_url: Optional[str] = None

    @staticmethod
    def _build_apply_url(job_url: str) -> str:
        """Rewrite .../job/<id> → .../job/<id>/apply on Jobvite hosts only.

        Non-Jobvite hosts (company careers domains that merely embed Jobvite)
        use different routing — return unchanged and let the Apply-CTA click
        below (or the AgentLoop) find the entry point.
        """
        parsed = urlparse(job_url)
        host = (parsed.hostname or "").lower()
        if "jobvite.com" not in host:
            return job_url
        path = parsed.path.rstrip("/")
        if path.endswith("/apply") or "/job/" not in path:
            return job_url
        return urlunparse(parsed._replace(path=path + "/apply"))

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        target = self._build_apply_url(job_url)
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.warning(f"[Jobvite] goto {target!r} failed ({exc}); retrying base URL")
            if target != job_url:
                try:
                    await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
                except Exception:
                    pass
        self._resolved_url = page.url

        # If we are not on the form yet, click the Apply anchor.
        if not await self._form_present(page):
            learned = get_learned_fixes(self.platform_name).get("apply_button")
            for sel in learned + [s for s in _APPLY_SELECTORS if s not in learned]:
                try:
                    el = page.locator(sel).first
                    if await el.count() == 0 or not await el.is_visible():
                        continue
                    await el.scroll_into_view_if_needed()
                    await el.click(timeout=5_000)
                    await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                    get_learned_fixes(self.platform_name).add("apply_button", sel)
                    logger.info(f"[Jobvite] Apply clicked via {sel!r}")
                    break
                except Exception as exc:
                    logger.debug(f"[Jobvite] apply CTA {sel!r} failed: {exc}")
            self._resolved_url = page.url

        try:
            await page.locator("input[id^='jv-field-']").first.wait_for(
                state="attached", timeout=15_000
            )
            logger.info("[Jobvite] application form detected")
        except Exception:
            logger.warning(
                "[Jobvite] form fields not detected after 15s — "
                "AgentLoop will re-perceive from the live page"
            )
        await self.human_delay(0.5, 1.5)

    @staticmethod
    async def _form_present(page: Page) -> bool:
        try:
            return await page.locator("input[id^='jv-field-']").count() > 0
        except Exception:
            return False

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
        # Scripted fallback only — the AgentLoop is the primary driver.
        form = pre_detected_form or await detect_form(page, container_selector=None)
        fill_success = await fill_form(page, form, profile, screening_answers, candidate_id=candidate_id)

        if resume_path:
            uploaded = False
            for field in form.fields:
                if field.field_type == "file":
                    await upload_file(page, field.selector, resume_path)
                    uploaded = True
                    break
            if not uploaded:
                try:
                    fi = page.locator("#file-input-0, input[type='file']").first
                    if await fi.count() > 0:
                        await fi.set_input_files(resume_path)
                except Exception as exc:
                    logger.debug(f"[Jobvite] fallback resume upload failed: {exc}")
        return fill_success

    async def submit(self, page: Page) -> bool:
        learned = get_learned_fixes(self.platform_name).get("submit")
        for sel in learned + [s for s in _SUBMIT_SELECTORS if s not in learned]:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0:
                    continue
                is_disabled = await btn.evaluate(
                    "el => el.disabled === true || el.getAttribute('aria-disabled') === 'true'"
                )
                if is_disabled:
                    logger.warning(
                        f"[Jobvite] submit {sel!r} DISABLED — required field or "
                        "captcha incomplete. Trying next selector."
                    )
                    continue
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=6_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=12_000)
                except Exception:
                    pass
                await self.human_delay(1.0, 2.0)
                get_learned_fixes(self.platform_name).add("submit", sel)
                logger.info(f"[Jobvite] submit via {sel!r}")
                return True
            except Exception as exc:
                logger.debug(f"[Jobvite] submit selector {sel!r} failed: {exc}")
        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        try:
            content = (await page.content()).lower()
        except Exception:
            return False, None
        for pattern in _ALREADY_APPLIED_PATTERNS:
            if pattern in content:
                raise Exception(
                    "ALREADY_APPLIED: Jobvite indicates this candidate already "
                    "applied to this job. Expected outcome — no retry."
                )
        for pattern in _SUCCESS_PATTERNS:
            if pattern in content:
                return True, pattern
        return False, None
