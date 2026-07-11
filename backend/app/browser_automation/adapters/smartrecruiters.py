"""SmartRecruiters (jobs.smartrecruiters.com) adapter.

URL patterns::

    https://jobs.smartrecruiters.com/<Company>/<postingId>-<slug>        (posting)
    https://jobs.smartrecruiters.com/oneclick-ui/company/<Company>/
        publication/<uuid>?dcr_ci=<Company>                              (apply form)

The posting page's "I'm interested" CTA is a plain ``<a>`` whose href IS the
application form URL (the oneclick-ui publication route) — so navigation is
deterministic: read the href and go, no vision click required.

The oneclick-ui form itself is built from ``<spl-*>`` web components whose
fields live in SHADOW DOM. Playwright locators pierce open shadow roots, so
``get_by_label`` / css-id selectors work at fill time, but naive
``document.querySelectorAll`` DOM scrapes see almost nothing — which is why
this adapter follows "AI master / Playwright slave": it only navigates and
exposes portal knowledge via hints (adapters/hints.py "smartrecruiters"),
deferring all form-fill to the vision AgentLoop.

Form invariants verified live (2026-07-11, Syncreon + InPost tenants):
  - required: First name, Last name, Email, **Confirm your email** (must
    repeat the email), and a consent checkbox ``#noPolicy`` above Submit;
  - per-tenant required extras: City (autocomplete), Phone (country-code
    widget), Resume (spl-dropzone);
  - "Apply With Indeed" / "Apply with LinkedIn" import widgets must never be
    clicked (manual-fill-only policy, same as SSO).
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
    "a[href*='/oneclick-ui/']",
    "a:has-text(\"I'm interested\")",
    "button:has-text(\"I'm interested\")",
    "a:has-text('Interested')",
]

_SUBMIT_SELECTORS = [
    "button[type='submit']:has-text('Submit')",
    "button:has-text('Submit application')",
    "button:has-text('Submit')",
]

# Specific phrases only — a bare "thank you" appears on rejection banners and
# cookie notices too (see the 2026-07 false-positive-submit purge).
_SUCCESS_PATTERNS = (
    "application submitted",
    "thank you for applying",
    "thanks for applying",
    "your application was sent",
    "application received",
    "application is complete",
)

_ALREADY_APPLIED_PATTERNS = (
    "already applied",
    "you have already applied",
    "already submitted an application",
)


class SmartRecruitersAdapter(BasePlatformAdapter):
    platform_name = "smartrecruiters"
    container_selector = None  # oneclick-ui form is full page

    def __init__(self):
        self._iframe_mode: bool = False
        self._frame = None
        self._resolved_url: Optional[str] = None

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        if "/oneclick-ui/" not in (page.url or "") or page.url != job_url:
            try:
                await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                logger.warning(f"[SmartRecruiters] initial goto failed (non-fatal): {exc}")
        self._resolved_url = page.url

        if "/oneclick-ui/" in (page.url or ""):
            await self._wait_for_form(page)
            return

        # Posting page → CLICK the "I'm interested" control so SmartRecruiters'
        # SPA performs its own in-app navigation and freshly mints + hydrates
        # the oneclick-ui form. IMPORTANT: do NOT scrape the anchor href and
        # goto() it directly — the publication URL is session-bound/single-use
        # and a fresh navigation to a copied one returns "An error occurred,
        # please try again later" (verified live 2026-07-11). Clicking is the
        # only robust path (matches how the vision AgentLoop reaches it too).
        winning_sel: Optional[str] = None
        learned = get_learned_fixes(self.platform_name).get("apply_button")
        for sel in learned + [s for s in _APPLY_SELECTORS if s not in learned]:
            try:
                el = page.locator(sel).first
                if await el.count() == 0 or not await el.is_visible():
                    continue
                await el.scroll_into_view_if_needed()
                await el.click(timeout=6_000)
                try:
                    await page.wait_for_url("**/oneclick-ui/**", timeout=15_000)
                except Exception:
                    # Some tenants open the form in a NEW TAB — adopt it.
                    for p in page.context.pages:
                        if "/oneclick-ui/" in (p.url or ""):
                            page = p
                            break
                if "/oneclick-ui/" in (page.url or ""):
                    winning_sel = sel
                    break
            except Exception as exc:
                logger.debug(f"[SmartRecruiters] apply CTA {sel!r} failed: {exc}")

        self._resolved_url = page.url
        # Only record the learned fix once we verifiably reached the form.
        if winning_sel and "/oneclick-ui/" in (page.url or ""):
            get_learned_fixes(self.platform_name).add("apply_button", winning_sel)
        if "/oneclick-ui/" not in (page.url or ""):
            # Not fatal — the vision AgentLoop can still find the CTA itself.
            logger.warning(
                "[SmartRecruiters] could not resolve the oneclick-ui form "
                f"deterministically (url={page.url!r}); leaving it to the AgentLoop"
            )
            return
        await self._wait_for_form(page)

    @staticmethod
    async def _wait_for_form(page: Page) -> None:
        """Wait for the shadow-DOM form to hydrate (Playwright pierces it)."""
        try:
            await page.locator("#first-name-input").first.wait_for(
                state="attached", timeout=15_000
            )
            logger.info("[SmartRecruiters] oneclick-ui form hydrated")
        except Exception:
            logger.warning(
                "[SmartRecruiters] form fields not detected after 15s — "
                "page may still be hydrating; AgentLoop will re-perceive"
            )

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

        # The detector misses shadow-DOM fields, so patch the two SmartRecruiters
        # invariants it cannot see: confirm-email and the consent checkbox.
        email = (profile or {}).get("email") or ""
        if email:
            for sel in ("#email-input", "#confirm-email-input"):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and not (await loc.input_value()):
                        await loc.fill(email)
                except Exception:
                    pass
        try:
            consent = page.locator("#noPolicy").first
            if await consent.count() > 0 and not await consent.is_checked():
                await consent.check(timeout=3_000)
                logger.info("[SmartRecruiters] required consent checkbox checked")
        except Exception as exc:
            logger.debug(f"[SmartRecruiters] consent checkbox: {exc}")

        if resume_path:
            for field in form.fields:
                if field.field_type == "file" and "image" not in (field.label or "").lower():
                    await upload_file(page, field.selector, resume_path)
                    break
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
                        f"[SmartRecruiters] submit {sel!r} DISABLED — a required "
                        "field (often confirm-email or the consent checkbox) is "
                        "incomplete. Trying next selector."
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
                logger.info(f"[SmartRecruiters] submit via {sel!r}")
                return True
            except Exception as exc:
                logger.debug(f"[SmartRecruiters] submit selector {sel!r} failed: {exc}")
        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        try:
            content = (await page.content()).lower()
        except Exception:
            return False, None
        for pattern in _ALREADY_APPLIED_PATTERNS:
            if pattern in content:
                raise Exception(
                    "ALREADY_APPLIED: SmartRecruiters indicates this candidate "
                    "already applied to this job. Expected outcome — no retry."
                )
        for pattern in _SUCCESS_PATTERNS:
            if pattern in content:
                return True, pattern
        return False, None
