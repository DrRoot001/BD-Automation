"""ZipRecruiter adapter — 1-Click / Easy Apply (account-walled).

OPERATOR NOTE (one-time manual setup — NOT automatable):
    ZipRecruiter requires a US phone number at account registration and
    verifies it via SMS. The operator MUST create the seeker account
    manually (with a US number), then set ``ZIPRECRUITER_EMAIL`` and
    ``ZIPRECRUITER_PASSWORD`` in ``.env``. After the first successful
    automated login this adapter persists the full storage_state to
    ``backend/data/sessions/ziprecruiter.json``, which the browser context
    manager auto-restores — future runs skip the login entirely.

Apply model: ZipRecruiter's "1-Click Apply" submits with the pre-built
profile — clicking Apply on a job either (a) completes immediately with a
confirmation ("You've applied!"), or (b) opens an apply modal/form with
screening questions we fill via the shared detect_form/fill_form helpers.
Postings labelled "Apply on company site" are external and NOT driven here.

CAPTCHA: hCaptcha may gate the login — handed to CaptchaService with
captcha_type="hcaptcha".

Rate-limit caution: ZipRecruiter throttles aggressively (~5 applications/
hour/account observed) — the platform rate limiter should keep this low.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple

from playwright.async_api import Page

from .base import BasePlatformAdapter

logger = logging.getLogger(__name__)

_NAV_TIMEOUT_MS = 35_000
_FIELD_TIMEOUT_MS = 8_000

_LOGIN_URL = "https://www.ziprecruiter.com/login"

_APPLY_SELECTORS = (
    "button[data-testid='joblist-apply-button']",
    "button:has-text('1-Click Apply')",
    "button:has-text('Quick Apply')",
    "button:has-text('Apply')",
    "a:has-text('Apply Now')",
)

_MODAL_SELECTOR = "form[data-testid='apply-form'], .apply-modal"

# Visible-label markers that identify an EXTERNAL-apply CTA ("Apply on
# company site"). The broad button:has-text('Apply') selector would otherwise
# substring-match these and drive the employer ATS unintentionally.
_EXTERNAL_LABEL_MARKERS = ("company site", "employer site", "apply on")

_SUBMIT_SELECTORS = (
    "button:has-text('Apply Now')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit application')",
    "button[type='submit']",
)

_SUCCESS_TEXT_PATTERNS = (
    "applied successfully",
    "application submitted",
    "you've applied",
    "you have applied",
    "application sent",
    "your application has been sent",
    "thanks for applying",
    "thank you for applying",
    "application complete",
    "successfully applied",
)

# Login-form fields. ZipRecruiter's login may be single-page (email +
# password together) or stepped (email → Continue → password) depending on
# the build/AB test — both are handled.
_EMAIL_SELECTORS = (
    "input[name='email']",
    "input#email",
    "input[type='email']",
    "input[placeholder*='email' i]",
)
_PASSWORD_SELECTORS = (
    "input[name='password']",
    "input#password",
    "input[type='password']",
)
_EMAIL_CONTINUE_SELECTORS = (
    "button:has-text('Continue with email')",
    "button:has-text('Continue')",
    "button[type='submit']",
)
_SIGNIN_SELECTORS = (
    "button:has-text('Sign In')",
    "button:has-text('Sign in')",
    "button:has-text('Log In')",
    "button:has-text('Log in')",
    "button[type='submit']",
)

# Cookie-consent / promo-overlay dismissal (OneTrust is the common one —
# same pattern as builtin/dice).
_OVERLAY_SELECTORS = (
    "button#onetrust-accept-btn-handler",
    "button:has-text('Accept All Cookies')",
    "button:has-text('Accept All')",
    "button:has-text('Accept Cookies')",
    "button[aria-label='Close']",
    "button[aria-label='close']",
)


def _sessions_path(name: str) -> Path:
    # adapters/ → browser_automation/ → app/ → backend/ (matches the lookup
    # in browser/context_manager.py which auto-loads sessions/<platform>.json).
    backend_dir = Path(__file__).resolve().parents[3]
    return backend_dir / "data" / "sessions" / name


class ZipRecruiterAdapter(BasePlatformAdapter):
    platform_name = "ziprecruiter"
    container_selector = "form[data-testid='apply-form'], .apply-modal"

    def __init__(self) -> None:
        # True when the Apply click completed as a true 1-click apply (no
        # form/modal shown) — fill/submit then become no-ops.
        self._one_click_done: bool = False
        # Last apply-outcome ("one_click" | "form" | None). None means neither
        # a confirmation nor a form appeared — fill_application must NOT treat
        # that as a completed 1-click apply.
        self._apply_outcome: Optional[str] = None
        # Executor reads these via getattr — no iframe on ZipRecruiter.
        self._iframe_mode: bool = False
        self._frame_locator = None
        self._frame = None

    # ──────────────────────────────────────────────────────────────────────
    # Navigation
    # ──────────────────────────────────────────────────────────────────────

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        # 1. Authenticated session first — 1-Click Apply needs the profile.
        await self._ensure_logged_in(page)

        # 2. Open the job page.
        logger.info(f"[ZipRecruiter] Navigating to job {job_url!r}")
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        except Exception as exc:
            logger.warning(f"[ZipRecruiter] job goto soft-failed ({exc}); settling page")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8_000)
            except Exception:
                pass
        await self._dismiss_overlays(page)
        await self.human_delay(0.8, 1.6)

        # 3. Click the apply CTA.
        clicked = None
        for sel in _APPLY_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                # Reject external-apply CTAs ("Apply on company site") — the
                # broad button:has-text('Apply') substring-matches them, and
                # clicking would navigate to the employer ATS.
                label = ((await btn.inner_text()) or "").strip().lower()
                if any(m in label for m in _EXTERNAL_LABEL_MARKERS):
                    logger.debug(f"[ZipRecruiter] apply candidate {sel!r} label {label!r} is external; skipping")
                    continue
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=_FIELD_TIMEOUT_MS)
                clicked = sel
                logger.info(f"[ZipRecruiter] Apply clicked via {sel!r}")
                break
            except Exception as exc:
                logger.debug(f"[ZipRecruiter] apply candidate {sel!r} failed: {exc}")
        if not clicked:
            raise RuntimeError(
                "BLOCKED: ZipRecruiter apply button not found. Posting may be "
                "expired, external-apply only, or the session unauthenticated."
            )

        # 4. Wait for EITHER the 1-click confirmation OR the apply modal/form.
        outcome = await self._wait_apply_outcome(page, timeout_s=15.0)
        self._apply_outcome = outcome
        if outcome == "one_click":
            self._one_click_done = True
            logger.info("[ZipRecruiter] 1-Click Apply completed — no form to fill")
        elif outcome == "form":
            logger.info("[ZipRecruiter] Apply modal/form appeared — form-fill path")
        else:
            logger.warning(
                "[ZipRecruiter] Neither confirmation nor form appeared after "
                "Apply click — handing to fill/AgentLoop anyway"
            )
        await self.human_delay(0.6, 1.2)

    async def _wait_apply_outcome(self, page: Page, timeout_s: float = 15.0) -> Optional[str]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            # Form/modal takes priority — screening questions mean more work.
            try:
                modal = page.locator(_MODAL_SELECTOR).first
                if await modal.count() > 0 and await modal.is_visible():
                    return "form"
            except Exception:
                pass
            content = (await self._safe_content(page)).lower()
            if any(p in content for p in _SUCCESS_TEXT_PATTERNS):
                return "one_click"
            await asyncio.sleep(0.6)
        return None

    async def detect_application_type(self, page: Page) -> str:
        return "EASY_APPLY"

    # ──────────────────────────────────────────────────────────────────────
    # Authentication
    # ──────────────────────────────────────────────────────────────────────

    async def _ensure_logged_in(self, page: Page) -> None:
        """Log in unless a restored storage_state session is already live."""
        try:
            await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        except Exception as exc:
            logger.warning(f"[ZipRecruiter] login-page goto soft-failed ({exc})")
        await self._dismiss_overlays(page)

        # An authenticated user gets redirected off /login — poll briefly.
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if "/login" not in page.url.lower():
                logger.info(f"[ZipRecruiter] Existing session is authenticated (url={page.url!r})")
                return
            if await self._first_visible(page, _EMAIL_SELECTORS) is not None:
                break
            await asyncio.sleep(0.5)

        if "/login" not in page.url.lower():
            logger.info(f"[ZipRecruiter] Existing session is authenticated (url={page.url!r})")
            return

        await self._perform_login(page)

    async def _perform_login(self, page: Page) -> None:
        email = os.getenv("ZIPRECRUITER_EMAIL", "").strip()
        password = os.getenv("ZIPRECRUITER_PASSWORD", "").strip()
        if not email or not password:
            raise RuntimeError(
                "BLOCKED: ZIPRECRUITER_EMAIL / ZIPRECRUITER_PASSWORD not "
                "configured — cannot log in to ZipRecruiter. The account must "
                "be created MANUALLY by the operator (US phone verification)."
            )

        logger.info(f"[ZipRecruiter] Logging in as {email!r}")

        if not await self._fill_first(page, _EMAIL_SELECTORS, email):
            raise RuntimeError("BLOCKED: ZipRecruiter login email field not found")
        await self.human_delay(0.3, 0.8)

        # Stepped variant: password only renders after a Continue click.
        if await self._first_visible(page, _PASSWORD_SELECTORS) is None:
            await self._click_first(page, _EMAIL_CONTINUE_SELECTORS)
            await self._wait_visible(page, _PASSWORD_SELECTORS, timeout_ms=15_000)

        if not await self._fill_first(page, _PASSWORD_SELECTORS, password):
            raise RuntimeError("BLOCKED: ZipRecruiter login password field not found")
        await self.human_delay(0.3, 0.8)

        # hCaptcha may gate the sign-in — best-effort solve before submit.
        await self._maybe_solve_hcaptcha(page)
        await self._click_first(page, _SIGNIN_SELECTORS)

        logged_in = await self._wait_logged_in(page, timeout_s=25.0)
        if not logged_in:
            # Challenge may only appear after the first submit — one retry.
            if await self._maybe_solve_hcaptcha(page):
                await self._click_first(page, _SIGNIN_SELECTORS)
                logged_in = await self._wait_logged_in(page, timeout_s=20.0)

        if not logged_in:
            raise RuntimeError(
                "BLOCKED: ZipRecruiter login did not complete (still on the "
                f"login page: url={page.url!r}). May require an email code or "
                "an hCaptcha the solver could not pass."
            )
        logger.info(f"[ZipRecruiter] Login OK (url={page.url!r})")
        await self._save_storage_state(page)

    async def _wait_logged_in(self, page: Page, timeout_s: float = 25.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            url = page.url.lower()
            if "/login" not in url:
                return True
            content = (await self._safe_content(page)).lower()
            if ("incorrect" in content or "invalid" in content) and "password" in content:
                logger.error("[ZipRecruiter] Login rejected: incorrect email/password")
                return False
            await asyncio.sleep(1.0)
        return False

    async def _maybe_solve_hcaptcha(self, page: Page) -> bool:
        """Detect an hCaptcha and try to solve it. Returns True if attempted."""
        try:
            has_hcaptcha = await page.locator(
                "iframe[src*='hcaptcha'], .h-captcha[data-sitekey]"
            ).count() > 0
        except Exception:
            return False
        if not has_hcaptcha:
            return False

        logger.info("[ZipRecruiter] hCaptcha detected; attempting solve")
        try:
            from ..captcha import CaptchaService
            provider = os.getenv("CAPTCHA_PROVIDER", "ai").lower()
            solution = await CaptchaService(provider=provider).solve(page, "hcaptcha")
            logger.info(f"[ZipRecruiter] hCaptcha solve success={getattr(solution, 'success', False)}")
        except Exception as exc:
            logger.warning(f"[ZipRecruiter] hCaptcha solve failed (non-fatal): {exc}")
        return True

    async def _save_storage_state(self, page: Page) -> None:
        """Persist cookies + localStorage so future runs skip login entirely
        (backend/data/sessions/ziprecruiter.json — auto-loaded by the
        context manager)."""
        try:
            path = _sessions_path("ziprecruiter.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.context.storage_state(path=str(path))
            logger.info(f"[ZipRecruiter] storage_state saved → {path}")
        except Exception as exc:
            logger.warning(f"[ZipRecruiter] storage_state save failed (non-fatal): {exc}")

    # ──────────────────────────────────────────────────────────────────────
    # Fill / submit / verify
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
        if self._one_click_done:
            # True 1-click apply — the profile WAS the application.
            logger.info("[ZipRecruiter] 1-click already applied — nothing to fill")
            return True

        try:
            modal = page.locator(_MODAL_SELECTOR).first
            has_form = await modal.count() > 0 and await modal.is_visible()
        except Exception:
            has_form = False
        if not has_form and pre_detected_form is None:
            if self._apply_outcome is None:
                # Neither a confirmation nor a form appeared after the Apply
                # click — do NOT treat as a completed 1-click apply (submit()
                # would otherwise fall back to clicking button[type=submit],
                # potentially on an unfilled external ATS form).
                logger.warning(
                    "[ZipRecruiter] No apply form present and no confirmation "
                    "seen — not assuming 1-click apply"
                )
                return False
            # No form materialised (some postings confirm without any text we
            # matched) — treat as 1-click rather than failing the fill step.
            logger.info("[ZipRecruiter] No apply form present — treating as 1-click apply")
            return True

        from ..forms import detect_form, fill_form

        form = pre_detected_form or await detect_form(page, container_selector=self.container_selector)
        return await fill_form(page, form, profile, screening_answers, candidate_id=candidate_id)

    async def submit(self, page: Page) -> bool:
        if self._one_click_done:
            return True

        # Scope to the modal/form so we never re-click the page-level Apply.
        try:
            modal = page.locator(_MODAL_SELECTOR).first
            scope = modal if (await modal.count() > 0 and await modal.is_visible()) else page
        except Exception:
            scope = page
        for sel in _SUBMIT_SELECTORS:
            try:
                btn = scope.locator(sel).first
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=_FIELD_TIMEOUT_MS)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                await self.human_delay(1.0, 2.0)
                logger.info(f"[ZipRecruiter] Submitted via {sel!r}")
                return True
            except Exception as exc:
                logger.debug(f"[ZipRecruiter] submit selector {sel!r} failed: {exc}")
        logger.error("[ZipRecruiter] No submit button matched")
        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        content = (await self._safe_content(page)).lower()
        for pattern in _SUCCESS_TEXT_PATTERNS:
            if pattern in content:
                logger.info(f"[ZipRecruiter] Success verified via text {pattern!r}")
                return (True, pattern)
        # A completed 1-click whose confirmation toast already disappeared.
        if self._one_click_done:
            logger.info("[ZipRecruiter] Success assumed via completed 1-click apply")
            return (True, "1-click apply confirmed")
        return (False, None)

    async def refresh_frame(self, page: Page) -> None:
        # No persistent iframe on ZipRecruiter.
        return None

    # ──────────────────────────────────────────────────────────────────────
    # Low-level helpers
    # ──────────────────────────────────────────────────────────────────────

    async def _dismiss_overlays(self, page: Page) -> None:
        for sel in _OVERLAY_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=2_000)
                    logger.info(f"[ZipRecruiter] Dismissed overlay via {sel!r}")
                    await asyncio.sleep(0.3)
                    break
            except Exception:
                continue

    async def _first_visible(self, page: Page, selectors) -> Optional[str]:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return sel
            except Exception:
                continue
        return None

    async def _fill_first(self, page: Page, selectors, value: str) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=3_000)
                    await loc.fill("")
                    await loc.fill(value)
                    return True
            except Exception:
                continue
        return False

    async def _click_first(self, page: Page, selectors) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible() and await loc.is_enabled():
                    await loc.click(timeout=4_000)
                    return True
            except Exception:
                continue
        return False

    async def _wait_visible(self, page: Page, selectors, timeout_ms: int = 8_000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            if await self._first_visible(page, selectors) is not None:
                return True
            await asyncio.sleep(0.4)
        return False

    @staticmethod
    async def _safe_content(page: Page) -> str:
        try:
            return await page.content()
        except Exception:
            return ""
