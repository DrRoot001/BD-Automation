"""Glassdoor adapter — Easy Apply modal OR external-ATS passthrough.

Glassdoor postings come in two flavours:

  a. **Easy Apply** — ``button[data-test='applyButtonGDP']`` (or an "Easy
     Apply" labelled button) opens an in-page application modal
     (``.modal-content`` / ``[data-test='JobApplicationModal']``). We fill
     that modal with the shared detect_form/fill_form helpers, scoped to
     the modal container.

  b. **External redirect** ("Apply on company site") — the apply CTA links
     out to the employer's real ATS (Greenhouse / Lever / Workday / …).
     We resolve that URL, detect the ATS with the same
     ``_detect_ats_from_url`` used by RemoteRocketship, and DELEGATE every
     subsequent adapter method to the inner ATS adapter — identical to the
     RemoteRocketshipAdapter passthrough pattern.

Auth: Glassdoor is ACCOUNT-WALLED for Easy Apply. Login uses
``GLASSDOOR_EMAIL`` / ``GLASSDOOR_PASSWORD`` env vars against
https://www.glassdoor.com/profile/login_input.htm. Glassdoor's login is a
2-step email-then-password form (like Dice), but some builds render both
fields on one page — both variants are handled defensively. A Cloudflare
Turnstile challenge may gate the login; we hand it to CaptchaService
(captcha_type="turnstile"). After a successful login the full
storage_state is persisted to ``backend/data/sessions/glassdoor.json`` so
the browser context manager auto-restores it on the next run (repeated
logins get bot-throttled — same precedent as Dice / LinkedIn).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .base import BasePlatformAdapter
from .remoterocketship import _detect_ats_from_url

logger = logging.getLogger(__name__)

_NAV_TIMEOUT_MS = 35_000
_FIELD_TIMEOUT_MS = 8_000

_LOGIN_URL = "https://www.glassdoor.com/profile/login_input.htm"

# Easy Apply CTA on the job page (best-first). data-test is the stable hook;
# the text fallbacks cover older builds.
_EASY_APPLY_SELECTORS = (
    "button[data-test='applyButtonGDP']",
    "button[data-test='applyButton']",
    "button:has-text('Easy Apply')",
)
# External-apply CTA (links out to the employer ATS).
_EXTERNAL_APPLY_SELECTORS = (
    "a[data-test='applyButton']",
    "a:has-text('Apply on company site')",
    "a:has-text('Apply on employer site')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply on company site')",
)

_MODAL_SELECTOR = ".modal-content, [data-test='JobApplicationModal']"

_SUBMIT_SELECTORS = (
    "button[data-test='submit-application']",
    "button:has-text('Submit application')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit')",
    "button[type='submit']",
)

_SUCCESS_TEXT_PATTERNS = (
    "application submitted",
    "your application has been submitted",
    "application sent",
    "thank you for applying",
    "we've received your application",
)

# Login-form fields. Glassdoor's login is usually email → Continue →
# password → Sign In, but the ids cover the single-page variant too.
_EMAIL_SELECTORS = (
    "input#inlineUserEmail",
    "input[name='username']",
    "input[type='email']",
    "input[placeholder*='email' i]",
)
_PASSWORD_SELECTORS = (
    "input#inlineUserPassword",
    "input[name='password']",
    "input[type='password']",
)
_EMAIL_CONTINUE_SELECTORS = (
    "button[data-test='email-form-button']",
    "button:has-text('Continue with email')",
    "button:has-text('Continue')",
    "button[type='submit']",
)
_PASSWORD_SUBMIT_SELECTORS = (
    "button[name='submit']",
    "button:has-text('Sign In')",
    "button:has-text('Sign in')",
    "button[type='submit']",
)

# Cookie-consent / overlay dismissal.
_COOKIE_SELECTORS = (
    "button#onetrust-accept-btn-handler",
    "button:has-text('Accept All Cookies')",
    "button:has-text('Accept All')",
    "button:has-text('Accept Cookies')",
)

# Cloudflare Turnstile presence markers (login + occasionally apply).
_TURNSTILE_SELECTORS = (
    "iframe[src*='challenges.cloudflare.com']",
    ".cf-turnstile[data-sitekey]",
    "[data-sitekey][class*='turnstile']",
)


def _sessions_path(name: str) -> Path:
    # adapters/ → browser_automation/ → app/ → backend/ (same derivation as
    # browser/context_manager.py, which auto-loads sessions/<platform>.json).
    backend_dir = Path(__file__).resolve().parents[3]
    return backend_dir / "data" / "sessions" / name


class GlassdoorAdapter(BasePlatformAdapter):
    platform_name = "glassdoor"
    container_selector = ".modal-content, [data-test='JobApplicationModal']"

    def __init__(self) -> None:
        # Set when the posting redirects to an external ATS — all methods
        # then delegate (RemoteRocketship passthrough pattern).
        self._inner: Optional[BasePlatformAdapter] = None
        self._resolved_url: Optional[str] = None
        self._native_modal: bool = False
        # Executor reads these via getattr — mirrored from the inner adapter.
        self._iframe_mode: bool = False
        self._frame_locator = None
        self._frame = None

    # ──────────────────────────────────────────────────────────────────────
    # Navigation
    # ──────────────────────────────────────────────────────────────────────

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        # Lazy import to avoid a circular import with registry.py (same
        # trick as remoterocketship.py / builtin.py).
        from .registry import get_adapter

        # 0. If the stored URL is already a resolved ATS URL (jobs.source is
        # just the 'glassdoor' label), skip Glassdoor entirely.
        direct_key = _detect_ats_from_url(job_url)
        hostname = (urlparse(job_url).hostname or "").lower()
        if direct_key and "glassdoor" not in hostname:
            self._inner = self._spawn_delegate(direct_key)
            self._resolved_url = job_url
            logger.info(f"[Glassdoor] URL is already a resolved {direct_key!r} ATS — delegating directly")
            await self._inner.navigate_to_application(page, job_url)
            self._mirror_inner_attrs()
            return

        # 1. Authenticated session first — Easy Apply requires it.
        await self._ensure_logged_in(page)

        # 2. Open the job page.
        logger.info(f"[Glassdoor] Navigating to job {job_url!r}")
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        except Exception as exc:
            logger.warning(f"[Glassdoor] job goto soft-failed ({exc}); settling page")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8_000)
            except Exception:
                pass
        await self._dismiss_overlays(page)
        await self.human_delay(0.8, 1.6)

        # 3a. Easy Apply branch — click and wait for the modal.
        if await self._click_easy_apply(page):
            self._native_modal = True
            logger.info("[Glassdoor] Easy Apply modal opened — native fill path")
            return

        # 3b. External branch — resolve the employer-ATS URL and delegate.
        resolved = await self._resolve_external_url(page)
        if resolved:
            ats_key = _detect_ats_from_url(resolved) or "generic"
            self._inner = self._spawn_delegate(ats_key)
            self._resolved_url = resolved
            logger.info(f"[Glassdoor] Delegating to {ats_key!r} adapter for {resolved!r}")
            try:
                await page.goto(resolved, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            except Exception as exc:
                logger.warning(f"[Glassdoor] goto resolved URL failed ({exc}); inner adapter will retry")
            await self._inner.navigate_to_application(page, resolved)
            self._mirror_inner_attrs()
            return

        # 3c. Nothing matched — fall through to generic so the AgentLoop's
        # vision agent can salvage whatever is on-screen.
        logger.warning(f"[Glassdoor] No Easy Apply modal or external ATS link found on {job_url!r}")
        self._inner = self._spawn_delegate("generic")
        self._mirror_inner_attrs()

    async def detect_application_type(self, page: Page) -> str:
        if self._native_modal:
            return "EASY_APPLY"
        if self._inner:
            try:
                return await self._inner.detect_application_type(page)
            except NotImplementedError:
                pass
        return "EXTERNAL"

    # ──────────────────────────────────────────────────────────────────────
    # Authentication
    # ──────────────────────────────────────────────────────────────────────

    async def _ensure_logged_in(self, page: Page) -> None:
        """Log in unless a restored storage_state session is already live."""
        try:
            await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        except Exception as exc:
            logger.warning(f"[Glassdoor] login-page goto soft-failed ({exc})")
        await self._dismiss_overlays(page)
        await self._maybe_solve_turnstile(page)

        # An authenticated user gets bounced off the login page (to /member/
        # home or similar) — poll briefly before deciding we must log in.
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if "login" not in page.url.lower():
                logger.info(f"[Glassdoor] Existing session is authenticated (url={page.url!r})")
                return
            if await self._first_visible(page, _EMAIL_SELECTORS) is not None:
                break
            await asyncio.sleep(0.5)

        if "login" not in page.url.lower():
            logger.info(f"[Glassdoor] Existing session is authenticated (url={page.url!r})")
            return

        await self._perform_login(page)

    async def _perform_login(self, page: Page) -> None:
        email = self._login_credential("login_email", "GLASSDOOR_EMAIL")
        password = self._login_credential("password", "GLASSDOOR_PASSWORD")
        if not email or not password:
            raise RuntimeError(
                "BLOCKED: no Glassdoor credentials — set GLASSDOOR_EMAIL / "
                "GLASSDOOR_PASSWORD or add gmail+password to the candidate profile "
                "(Easy Apply requires an account)."
            )

        logger.info(f"[Glassdoor] Logging in as {email!r}")

        # Step 1 — email. On the stepped variant the password field only
        # renders AFTER "Continue"; on the single-page variant both fields
        # are present at once — handle both by checking for the password
        # field before clicking Continue.
        if not await self._fill_first(page, _EMAIL_SELECTORS, email):
            raise RuntimeError("BLOCKED: Glassdoor login email field not found")
        await self.human_delay(0.3, 0.8)

        if await self._first_visible(page, _PASSWORD_SELECTORS) is None:
            # Stepped variant — advance to the password screen.
            await self._click_first(page, _EMAIL_CONTINUE_SELECTORS)
            await self._wait_visible(page, _PASSWORD_SELECTORS, timeout_ms=15_000)

        if not await self._fill_first(page, _PASSWORD_SELECTORS, password):
            raise RuntimeError("BLOCKED: Glassdoor login password field not found")
        await self.human_delay(0.3, 0.8)

        # Turnstile may gate the submit — best-effort solve before clicking.
        await self._maybe_solve_turnstile(page)
        await self._click_first(page, _PASSWORD_SUBMIT_SELECTORS)

        logged_in = await self._wait_logged_in(page, timeout_s=25.0)
        if not logged_in:
            # A challenge may only appear after the first submit — one retry.
            if await self._maybe_solve_turnstile(page):
                await self._click_first(page, _PASSWORD_SUBMIT_SELECTORS)
                logged_in = await self._wait_logged_in(page, timeout_s=20.0)

        if not logged_in:
            # Clear the persisted session so a stale/corrupt blob doesn't wedge
            # every future run — next attempt starts from a clean fresh-login.
            try:
                from .session_utils import invalidate_session_file
                invalidate_session_file("glassdoor")
            except Exception:
                pass
            raise RuntimeError(
                "BLOCKED: Glassdoor login did not complete (still on a login/"
                f"challenge page: url={page.url!r}). May require MFA, an email "
                "code, or a Turnstile the solver could not pass."
            )
        logger.info(f"[Glassdoor] Login OK (url={page.url!r})")
        await self._save_storage_state(page)

    async def _wait_logged_in(self, page: Page, timeout_s: float = 25.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            url = page.url.lower()
            if "login" not in url and "authenticate" not in url:
                return True
            content = (await self._safe_content(page)).lower()
            if ("incorrect" in content or "invalid" in content) and "password" in content:
                logger.error("[Glassdoor] Login rejected: incorrect email/password")
                return False
            await asyncio.sleep(1.0)
        return False

    async def _maybe_solve_turnstile(self, page: Page) -> bool:
        """Detect a Cloudflare Turnstile and try to solve it. Returns True if attempted."""
        found = False
        for sel in _TURNSTILE_SELECTORS:
            try:
                if await page.locator(sel).count() > 0:
                    found = True
                    break
            except Exception:
                continue
        if not found:
            return False

        logger.info("[Glassdoor] Cloudflare Turnstile detected; attempting solve")
        try:
            from ..captcha import CaptchaService
            provider = os.getenv("CAPTCHA_PROVIDER", "ai").lower()
            solution = await CaptchaService(provider=provider).solve(page, "turnstile")
            logger.info(f"[Glassdoor] Turnstile solve success={getattr(solution, 'success', False)}")
        except Exception as exc:
            logger.warning(f"[Glassdoor] Turnstile solve failed (non-fatal): {exc}")
        return True

    async def _save_storage_state(self, page: Page) -> None:
        """Persist cookies + localStorage so the context manager reuses the
        session on the next run (backend/data/sessions/glassdoor.json)."""
        try:
            path = _sessions_path("glassdoor.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.context.storage_state(path=str(path))
            logger.info(f"[Glassdoor] storage_state saved → {path}")
        except Exception as exc:
            logger.warning(f"[Glassdoor] storage_state save failed (non-fatal): {exc}")

    # ──────────────────────────────────────────────────────────────────────
    # Easy Apply / external resolution
    # ──────────────────────────────────────────────────────────────────────

    async def _click_easy_apply(self, page: Page) -> bool:
        """Click the Easy Apply CTA and confirm the modal opened."""
        for sel in _EASY_APPLY_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                # data-test='applyButtonGDP' is also used on external-apply
                # CTAs in some builds — only treat it as Easy Apply if the
                # visible label says so.
                label = ((await btn.inner_text()) or "").strip().lower()
                if "easy apply" not in label:
                    logger.debug(f"[Glassdoor] {sel!r} label {label!r} is not Easy Apply; skipping")
                    continue
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=_FIELD_TIMEOUT_MS)
                logger.info(f"[Glassdoor] Easy Apply clicked via {sel!r}")
                try:
                    await page.locator(_MODAL_SELECTOR).first.wait_for(state="visible", timeout=12_000)
                    return True
                except PlaywrightTimeoutError:
                    logger.warning("[Glassdoor] Easy Apply clicked but modal never appeared")
                    return False
            except Exception as exc:
                logger.debug(f"[Glassdoor] Easy Apply candidate {sel!r} failed: {exc}")
        return False

    async def _resolve_external_url(self, page: Page) -> Optional[str]:
        """Resolve the external employer-ATS URL for 'Apply on company site'.

        Order: (1) href straight off the apply anchor, (2) any page anchor
        that maps to a known ATS, (3) click the CTA and catch the new tab.
        """
        # 1. Direct href on the apply CTA.
        for sel in _EXTERNAL_APPLY_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0:
                    continue
                href = await loc.get_attribute("href")
                if href and href.startswith("http") and "glassdoor" not in (urlparse(href).hostname or ""):
                    logger.info(f"[Glassdoor] External apply href via {sel!r}: {href!r}")
                    return href
            except Exception:
                continue

        # 2. Any anchor on the page pointing at a known ATS host.
        try:
            hrefs = await page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".map(a => a.href).filter(h => h.startsWith('http'))"
            )
        except Exception:
            hrefs = []
        for href in hrefs or []:
            if _detect_ats_from_url(href):
                logger.info(f"[Glassdoor] External ATS anchor found: {href!r}")
                return href

        # 3. Click the CTA and catch the new tab (Glassdoor often opens the
        # employer site in a popup) — same dance as builtin.py. Only the
        # external-apply CTAs here: Easy Apply was already ruled out by
        # _click_easy_apply, and clicking it now would open the native modal.
        for sel in _EXTERNAL_APPLY_SELECTORS:
            new_page_future = None
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                ctx = page.context
                new_page_future = asyncio.ensure_future(ctx.wait_for_event("page", timeout=15_000))
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=_FIELD_TIMEOUT_MS)
                try:
                    new_page = await new_page_future
                    new_page_future = None
                except (asyncio.TimeoutError, PlaywrightTimeoutError, asyncio.CancelledError):
                    # No popup — maybe same-tab redirect off glassdoor.
                    await self.human_delay(1.0, 2.0)
                    if "glassdoor" not in (urlparse(page.url).hostname or ""):
                        logger.info(f"[Glassdoor] Same-tab external redirect: {page.url!r}")
                        return page.url
                    continue
                try:
                    await new_page.wait_for_load_state("domcontentloaded", timeout=15_000)
                except Exception:
                    pass
                resolved = new_page.url
                # The executor keeps driving the ORIGINAL page — close the
                # popup, we re-goto the resolved URL in the main page.
                try:
                    await new_page.close()
                except Exception:
                    pass
                if resolved and resolved.startswith("http"):
                    logger.info(f"[Glassdoor] External URL via new tab: {resolved!r}")
                    return resolved
            except Exception as exc:
                logger.debug(f"[Glassdoor] external-click candidate {sel!r} failed: {exc}")
            finally:
                # Cancel an un-awaited page-event future so it never surfaces
                # later as an unretrieved-task TimeoutError in the loop.
                if new_page_future is not None and not new_page_future.done():
                    new_page_future.cancel()
        return None

    def _mirror_inner_attrs(self) -> None:
        if not self._inner:
            return
        self._iframe_mode = getattr(self._inner, "_iframe_mode", False)
        self._frame_locator = getattr(self._inner, "_frame_locator", None)
        self._frame = getattr(self._inner, "_frame", None)

    # ──────────────────────────────────────────────────────────────────────
    # Fill / submit / verify — native modal or delegation.
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
        if self._inner:
            return await self._inner.fill_application(
                page, profile, resume_path, cover_letter_path,
                screening_answers, pre_detected_form=pre_detected_form,
                candidate_id=candidate_id,
            )
        from ..forms import detect_form, fill_form

        form = pre_detected_form or await detect_form(page, container_selector=self.container_selector)
        return await fill_form(page, form, profile, screening_answers, candidate_id=candidate_id)

    async def submit(self, page: Page) -> bool:
        if self._inner:
            return await self._inner.submit(page)

        # Scope submit to the modal so we never hit the page-level Apply CTA.
        modal = page.locator(_MODAL_SELECTOR).first
        scope = modal if await self._safe_count(modal) > 0 else page
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
                logger.info(f"[Glassdoor] Submitted via {sel!r}")
                return True
            except Exception as exc:
                logger.debug(f"[Glassdoor] submit selector {sel!r} failed: {exc}")
        logger.error("[Glassdoor] No submit button matched")
        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        if self._inner:
            return await self._inner.verify_success(page)
        content = (await self._safe_content(page)).lower()
        for pattern in _SUCCESS_TEXT_PATTERNS:
            if pattern in content:
                logger.info(f"[Glassdoor] Success verified via text {pattern!r}")
                return (True, pattern)
        return (False, None)

    async def refresh_frame(self, page: Page) -> None:
        if self._inner and hasattr(self._inner, "refresh_frame"):
            await self._inner.refresh_frame(page)
            self._mirror_inner_attrs()

    # ──────────────────────────────────────────────────────────────────────
    # Low-level helpers
    # ──────────────────────────────────────────────────────────────────────

    async def _dismiss_overlays(self, page: Page) -> None:
        for sel in _COOKIE_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=2_000)
                    logger.info(f"[Glassdoor] Dismissed overlay via {sel!r}")
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
    async def _safe_count(locator) -> int:
        try:
            return await locator.count()
        except Exception:
            return 0

    @staticmethod
    async def _safe_content(page: Page) -> str:
        try:
            return await page.content()
        except Exception:
            return ""
