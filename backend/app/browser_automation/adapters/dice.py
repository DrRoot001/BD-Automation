"""Dice.com Easy Apply adapter.

Dice Easy Apply is ACCOUNT-WALLED: the application wizard
(``/job-applications/<id>/wizard``) only exists for a logged-in Dice seeker
account. There is no anonymous path. This adapter therefore owns two
deterministic concerns the AgentLoop should NOT drive:

  1. **Authentication** — log in with ``DICE_EMAIL`` / ``DICE_PASSWORD`` (or
     reuse a persisted Redis session) before touching the job page. Login is
     infrastructure, not form-fill, and credentials must never reach the
     vision LLM prompt — same precedent as LinkedIn / Workday.
  2. **Easy Apply detection + wizard entry** — verify the "Easy apply" CTA
     exists/visible/enabled, click it, and confirm we landed on the wizard URL.

Everything AFTER the wizard loads (Step 1 Resume & Cover Letter → Step 2
Additional Information → Step 3 Review → Submit) is driven by the AgentLoop
using the rich ``dice`` entry in ``adapters/hints.py``. The adapter only
provides deterministic ``submit`` / ``verify_success`` as a safety-net
fallback if the AgentLoop ever bails to the scripted pipeline.

URL shapes (per spec — never rely on application ids / dynamic query params):
  - Job detail : https://www.dice.com/job-detail/<uuid>
  - Wizard     : https://www.dice.com/job-applications/<id>/wizard
  - Success    : https://www.dice.com/job-applications/<id>/wizard/success
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Optional, Tuple

from playwright.async_api import Page

from .base import BasePlatformAdapter

logger = logging.getLogger(__name__)

_NAV_TIMEOUT_MS = 35_000
_FIELD_TIMEOUT_MS = 8_000

_LOGIN_URL = "https://www.dice.com/dashboard/login"

# Spec-mandated URL patterns. We match on path so query params / ids never
# leak into the detection logic.
_WIZARD_URL_RE = re.compile(r"/job-applications/.+/wizard", re.IGNORECASE)
_SUCCESS_URL_RE = re.compile(r"/job-applications/.+/wizard/success", re.IGNORECASE)

# Easy Apply CTA on the job-detail page. IMPORTANT details:
#   - Dice renders it as an <a> LINK (role=link), NOT a <button>.
#   - On a FRESH posting the accessible name is "Easy Apply".
#   - Once a draft has been started (we entered the wizard before) the SAME CTA
#     becomes "Continue Application" — both link to /job-applications/<id>/wizard.
#   - It renders a few seconds after load, so the caller polls.
# We match the name EXACTLY (anchored) so we don't grab the "Easy Apply" badges
# in the similar-jobs sidebar (whose accessible names are other job titles).
# The strongest, label-independent signal is an href pointing at the wizard.
_EASY_APPLY_NAME_RE = re.compile(
    r"^\s*(easy apply|continue application|resume application|continue applying)\s*$",
    re.IGNORECASE,
)
_EASY_APPLY_FALLBACK_SELECTORS = (
    "a[href*='/job-applications/'][href*='wizard']",
    "a:has-text('Easy Apply')",
    "a:has-text('Continue Application')",
    "button:has-text('Easy Apply')",
    "apply-button-wc a, apply-button-wc button",
    "[data-cy='easyApplyBtn']",
    "[data-testid*='easy-apply']",
)
_EASY_APPLY_POLL_S = 18.0

# A non-Easy-Apply ("Apply now" → external company site) CTA. If only this is
# present the posting is NOT Easy Apply and we cannot drive it here.
_EXTERNAL_APPLY_NAME_RE = re.compile(r"apply now|apply on company", re.IGNORECASE)

# Final submit on the Review step (fallback only — AgentLoop normally submits).
_SUBMIT_NAME_RE = re.compile(r"submit application|submit|^apply$", re.IGNORECASE)
_SUBMIT_FALLBACK_SELECTORS = (
    "button[data-cy='submit-application']",
    "button:has-text('Submit Application')",
    "button:has-text('Submit application')",
    "button:has-text('Submit')",
    "button[type='submit']",
)

_SUCCESS_TEXT_PATTERNS = (
    "application submitted",
    "your application has been submitted",
    "application has been submitted",
    "thank you for applying",
    "we've received your application",
)

# Login-form field hints (deterministic login). Dice's login is a React app;
# email may be on its own step before the password appears.
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
    "input[placeholder*='password' i]",
)
# Step 1 (email screen) → "Continue with email". MUST be the email button, not
# the "Continue with Google/Apple" SSO buttons next to it.
_EMAIL_CONTINUE_SELECTORS = (
    "button:has-text('Continue with email')",
    "button[type='submit']",
)
# Step 2 (password screen, /dashboard/login/password) → "Sign In".
_PASSWORD_SUBMIT_SELECTORS = (
    "button:has-text('Sign In')",
    "button:has-text('Sign in')",
    "button[type='submit']",
)

# Cookie-consent / overlay dismissal (shared with login + job page).
_COOKIE_SELECTORS = (
    "button#onetrust-accept-btn-handler",
    "button:has-text('Accept All Cookies')",
    "button:has-text('Accept All')",
    "button:has-text('Accept')",
)

# Strings that mean "you are NOT authenticated".
_AUTH_WALL_HINTS = (
    "sign in to your account",
    "log in to apply",
    "please sign in",
)


class DiceAdapter(BasePlatformAdapter):
    platform_name = "dice"
    # The wizard form is a plain (non-iframe) page. Left unscoped (None) so the
    # scripted-fallback detect_form scans the whole page — the AgentLoop is the
    # primary path and does its own DOM scoping, so a brittle container selector
    # would only risk the fallback.
    container_selector = None

    def __init__(self) -> None:
        # Mirror the iframe attributes other adapters expose so the executor's
        # getattr() reads return None cleanly (Dice has no form iframe).
        self._iframe_mode: bool = False
        self._frame_locator = None
        self._frame = None

    # ──────────────────────────────────────────────────────────────────────
    # Navigation (auth + Easy Apply entry)
    # ──────────────────────────────────────────────────────────────────────

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        # 1. Ensure we have an authenticated Dice session BEFORE the job page.
        await self._ensure_logged_in(page)

        # 2. Open the job-detail page.
        logger.info(f"[Dice] Navigating to job {job_url!r}")
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        except Exception as exc:
            logger.warning(f"[Dice] job goto soft-failed ({exc}); settling page")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8_000)
            except Exception:
                pass

        await self._dismiss_overlays(page)

        # If the job page bounced us to login, the session was stale — log in
        # and retry the job navigation once.
        if "/login" in page.url.lower() or self._page_text_has(await self._safe_content(page), _AUTH_WALL_HINTS):
            logger.info("[Dice] Job page requires auth — logging in then retrying")
            await self._perform_login(page)
            await page.goto(job_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            await self._dismiss_overlays(page)

        await self.human_delay(0.8, 1.6)

        # 3. Locate + click the Easy Apply CTA. The wizard then loads on a new
        # URL — that is where the AgentLoop takes over.
        clicked = await self._click_easy_apply(page)
        if not clicked:
            # Distinguish "not Easy Apply" from "couldn't find any apply button"
            external = await self._has_external_apply(page)
            if external:
                raise RuntimeError(
                    "BLOCKED: Dice posting is external-apply only (no Easy Apply). "
                    "This adapter only drives Dice Easy Apply postings."
                )
            raise RuntimeError(
                "BLOCKED: Easy Apply button not found. Posting may be expired, "
                "session unauthenticated, or Dice DOM shifted."
            )

        # 4. Confirm we reached the wizard (URL pattern). Don't hard-fail — the
        # PageAgent/AgentLoop can still recover — but log loudly if missing.
        try:
            await page.wait_for_url(_WIZARD_URL_RE, timeout=15_000)
            logger.info(f"[Dice] Wizard reached: {page.url!r}")
        except Exception:
            logger.warning(
                f"[Dice] Wizard URL not confirmed within 15s (url={page.url!r}); "
                "handing to AgentLoop anyway"
            )
        await self.human_delay(0.6, 1.2)

        # 5. Fix the wizard's ACCOUNT-profile defaults before the AgentLoop
        # runs. Dice pre-fills the resume, work authorization and location
        # from the logged-in account's profile — which belongs to whoever
        # owns the Dice login, not necessarily this candidate. Every field
        # counts as "filled", so the loop's required-field gate can't catch
        # wrong-but-present values (a live run submitted the account owner's
        # resume, no cover letter and work-auth "Prefer Not to Answer").
        if _WIZARD_URL_RE.search(page.url or ""):
            try:
                await self._prepare_wizard(page)
            except Exception as exc:
                logger.warning(
                    f"[Dice] wizard preparation incomplete ({exc}) — AgentLoop "
                    "hints remain the backstop"
                )

    # ──────────────────────────────────────────────────────────────────────
    # Wizard preparation (deterministic; candidate data over account profile)
    # ──────────────────────────────────────────────────────────────────────

    # Candidate work_authorization_type → Dice's exact dropdown labels
    # (<select name="workAuthorization">, verified empirically 2026-07-06).
    _DICE_WORK_AUTH = {
        "us citizen": "US Citizen",
        "green card holder": "Green Card Holder",
        "h1b": "Have H1 Visa",
        "opt": "Employment Auth Document",
        "ead": "Employment Auth Document",
        "tn visa": "TN Permit Holder",
    }

    async def _upload_via_chooser(self, page: Page, trigger, path: str, tag: str) -> bool:
        """Click `trigger` and feed `path` to the resulting file chooser.
        Falls back to set_input_files on a bare file input (Dice's inputs have
        no id/name, so the chooser event is the reliable route)."""
        try:
            async with page.expect_file_chooser(timeout=6_000) as fc_info:
                await trigger.click(timeout=5_000)
            chooser = await fc_info.value
            await chooser.set_files(path)
            logger.info(f"[Dice] {tag} uploaded via file chooser: {os.path.basename(path)}")
            return True
        except Exception as exc:
            logger.debug(f"[Dice] {tag} chooser route failed ({exc}); trying bare input")
        try:
            loc = page.locator("input[type='file']").first
            if await loc.count() > 0:
                await loc.set_input_files(path, timeout=8_000)
                logger.info(f"[Dice] {tag} uploaded via bare file input: {os.path.basename(path)}")
                return True
        except Exception as exc:
            logger.warning(f"[Dice] {tag} upload failed on both routes: {exc}")
        return False

    async def _prepare_wizard(self, page: Page) -> None:
        resume_path = getattr(self, "resume_local_path", None)
        cover_path = getattr(self, "cover_letter_local_path", None)
        profile = getattr(self, "candidate_profile", None) or {}

        # A resumed draft ("Continue Application") reopens the wizard on the
        # REVIEW step — the step-1 document controls don't exist there. Go
        # Back first so the file fixes below always run from step 1.
        try:
            content = await self._safe_content(page)
            if "review your application" in content.lower():
                back_btn = page.locator("button:has-text('Back')").first
                if await back_btn.count() > 0 and await back_btn.is_visible():
                    await back_btn.click(timeout=5_000)
                    await self.human_delay(1.5, 2.5)
                    logger.info("[Dice] resumed draft on review step — went Back to step 1")
        except Exception as exc:
            logger.debug(f"[Dice] draft-resume check skipped: {exc}")

        # ── Step 1: replace the account-profile resume with the tailored one ──
        if resume_path and os.path.isfile(resume_path):
            try:
                menu_btn = page.locator("button[aria-label='File options']").first
                if await menu_btn.count() > 0 and await menu_btn.is_visible():
                    await menu_btn.click(timeout=5_000)
                    await self.human_delay(0.5, 1.0)
                    replace_item = page.locator(
                        "[role='menuitem']", has_text=re.compile(r"replace|upload", re.I)
                    ).first
                    if await replace_item.count() > 0:
                        ok = await self._upload_via_chooser(page, replace_item, resume_path, "resume")
                        if ok:
                            # Give Dice's upload processing a moment, then verify
                            # the card shows the new filename.
                            await asyncio.sleep(3.0)
                            base = os.path.basename(resume_path)[:20]
                            content = await self._safe_content(page)
                            if base.lower() in content.lower():
                                logger.info("[Dice] resume card now shows the tailored file")
                            else:
                                logger.warning("[Dice] resume filename not visible after replace — verify on review step")
                    else:
                        await page.keyboard.press("Escape")
                        logger.warning("[Dice] File-options menu had no replace/upload item")
                else:
                    logger.info("[Dice] no 'File options' menu — resume card layout differs; leaving to AgentLoop")
            except Exception as exc:
                logger.warning(f"[Dice] resume replace failed (non-fatal): {exc}")

        # ── Step 1: attach the cover letter (dropzone reveals a chooser) ──
        if cover_path and os.path.isfile(cover_path):
            try:
                drop_btn = page.locator(
                    "button:has-text('Upload your cover letter'), "
                    "button:has-text('cover letter')"
                ).first
                if await drop_btn.count() > 0 and await drop_btn.is_visible():
                    await self._upload_via_chooser(page, drop_btn, cover_path, "cover letter")
                    await asyncio.sleep(2.0)
                else:
                    logger.info("[Dice] no cover-letter dropzone on this wizard")
            except Exception as exc:
                logger.warning(f"[Dice] cover-letter upload failed (non-fatal): {exc}")

        # ── Advance to the review step (exact 'Next' only — never Submit) ──
        advanced = False
        try:
            for b in await page.locator("button").all():
                if not await b.is_visible():
                    continue
                txt = (await b.inner_text() or "").strip().lower()
                if re.fullmatch(r"next( step)?|continue", txt):
                    await b.click(timeout=5_000)
                    advanced = True
                    break
        except Exception as exc:
            logger.warning(f"[Dice] could not advance to review step: {exc}")
        if not advanced:
            return
        await self.human_delay(2.0, 3.5)

        # ── Review step: force Work Authorization to the candidate's value ──
        desired_raw = (profile.get("work_authorization_type") or "").strip()
        desired = self._DICE_WORK_AUTH.get(desired_raw.lower())
        if not desired and (profile.get("work_authorization") or "").lower() == "yes":
            desired = "US Citizen"
        if desired:
            try:
                card_text = await page.evaluate(r"""() => {
                    const el = Array.from(document.querySelectorAll('section,article,div'))
                        .find(e => /^Work Authorization/.test((e.innerText||'').trim())
                                   && (e.innerText||'').length < 200);
                    return el ? el.innerText : '';
                }""")
                if desired.lower() not in (card_text or "").lower():
                    opened = await page.evaluate(r"""() => {
                        const blocks = Array.from(document.querySelectorAll('section,article,div'))
                            .filter(e => /^Work Authorization/.test((e.innerText||'').trim())
                                         && (e.innerText||'').length < 200);
                        for (const b of blocks) {
                            const btn = b.querySelector('button');
                            if (btn) { btn.click(); return true; }
                        }
                        return false;
                    }""")
                    if opened:
                        sel = page.locator("select[name='workAuthorization']").first
                        await sel.wait_for(state="attached", timeout=8_000)
                        await sel.select_option(label=desired)
                        logger.info(f"[Dice] work authorization set → {desired!r}")
                        # Location shares this edit form. Only touch it when the
                        # candidate has a real 'City, ST' style value — never
                        # overwrite with a bare country code like 'US'.
                        loc_val = (profile.get("location") or "").strip()
                        if "," in loc_val and len(loc_val) >= 6:
                            try:
                                loc_input = page.locator(
                                    "input[placeholder*='city or postal' i]"
                                ).first
                                if await loc_input.count() > 0 and await loc_input.is_visible():
                                    await loc_input.fill("")
                                    await loc_input.type(loc_val, delay=40)
                                    await asyncio.sleep(2.0)
                                    opt = page.locator("[role='option']").first
                                    if await opt.count() > 0 and await opt.is_visible():
                                        await opt.click(timeout=4_000)
                                        logger.info(f"[Dice] location set → {loc_val!r}")
                            except Exception as exc:
                                logger.warning(f"[Dice] location edit skipped: {exc}")
                        update_btn = page.locator("button:has-text('Update')").first
                        if await update_btn.count() > 0 and await update_btn.is_visible():
                            await update_btn.click(timeout=5_000)
                            await self.human_delay(1.5, 2.5)
                            logger.info("[Dice] review-card edits saved")
                else:
                    logger.info(f"[Dice] work authorization already correct ({desired!r})")
            except Exception as exc:
                logger.warning(f"[Dice] work-authorization fix failed (non-fatal): {exc}")

    async def detect_application_type(self, page: Page) -> str:
        return "EASY_APPLY"

    async def refresh_frame(self, page: Page) -> None:
        # No persistent iframe to track.
        return None

    # ──────────────────────────────────────────────────────────────────────
    # Authentication
    # ──────────────────────────────────────────────────────────────────────

    async def _ensure_logged_in(self, page: Page) -> None:
        """Log in to Dice unless a restored session is already authenticated."""
        try:
            await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        except Exception as exc:
            logger.warning(f"[Dice] login-page goto soft-failed ({exc})")
        await self._dismiss_overlays(page)

        # Dice shows a "Checking your session…" loader before EITHER rendering
        # the email form OR redirecting an already-authenticated user to the
        # dashboard. Wait for one of those outcomes (don't decide during the
        # loader — that was the original "email field not found" bug).
        email_appeared = await self._wait_login_resolved(page, timeout_s=20.0)
        if not email_appeared and "/login" not in page.url.lower():
            logger.info(f"[Dice] Existing session is authenticated (url={page.url!r})")
            return

        await self._perform_login(page)

    async def _wait_login_resolved(self, page: Page, timeout_s: float = 20.0) -> bool:
        """Wait for the login loader to resolve. Returns True if the email field
        appeared (need to log in), False if we navigated off /login (already
        authenticated)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if "/login" not in page.url.lower():
                return False
            if await self._email_field_visible(page):
                return True
            await asyncio.sleep(0.5)
        return await self._email_field_visible(page)

    async def _perform_login(self, page: Page) -> None:
        email = self._login_credential("login_email", "DICE_EMAIL")
        password = self._login_credential("password", "DICE_PASSWORD")
        if not email or not password:
            raise RuntimeError(
                "BLOCKED: no Dice credentials — set DICE_EMAIL / DICE_PASSWORD or add "
                "gmail+password to the candidate profile (Easy Apply requires an "
                "authenticated seeker account)."
            )

        if "/login" not in page.url.lower():
            try:
                await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                await self._dismiss_overlays(page)
            except Exception:
                pass

        logger.info(f"[Dice] Logging in as {email!r}")

        # Step 1 — email screen. Wait for the field to render (post-loader),
        # then fill it and click "Continue with email".
        if not await self._wait_visible(page, _EMAIL_SELECTORS, timeout_ms=20_000):
            # Maybe the session resolved to logged-in while we waited.
            if "/login" not in page.url.lower():
                logger.info("[Dice] Session resolved to authenticated during login wait")
                return
            raise RuntimeError("BLOCKED: Dice login email field not found")
        if not await self._fill_first(page, _EMAIL_SELECTORS, email):
            raise RuntimeError("BLOCKED: Dice login email field not fillable")
        await self.human_delay(0.3, 0.8)
        await self._click_first(page, _EMAIL_CONTINUE_SELECTORS)

        # Step 2 — password screen (/dashboard/login/password).
        if not await self._wait_visible(page, _PASSWORD_SELECTORS, timeout_ms=15_000):
            content = (await self._safe_content(page)).lower()
            if "couldn't find" in content or "could not find" in content or "create account" in content or "create an account" in content:
                raise RuntimeError(f"BLOCKED: No Dice account exists for email {email!r}. Please create one.")
            raise RuntimeError("BLOCKED: Dice login password field not found (Timeout waiting for password screen)")
            
        if not await self._fill_first(page, _PASSWORD_SELECTORS, password):
            raise RuntimeError("BLOCKED: Dice login password field not fillable")
        await self.human_delay(0.3, 0.8)

        # Best-effort captcha solve if Dice gates the login behind one.
        await self._maybe_solve_captcha(page)

        # Submit ("Sign In").
        await self._click_first(page, _PASSWORD_SUBMIT_SELECTORS)

        # Wait until we leave the login page (success) — bounded.
        logged_in = await self._wait_logged_in(page, timeout_s=25.0)
        if not logged_in:
            # One more captcha attempt + resubmit in case a challenge appeared
            # only after the first submit.
            if await self._maybe_solve_captcha(page):
                await self._click_first(page, _PASSWORD_SUBMIT_SELECTORS)
                logged_in = await self._wait_logged_in(page, timeout_s=20.0)

        if not logged_in:
            # Clear the persisted session: if a stale/corrupt blob contributed
            # to the failed login, leaving it in place would wedge every future
            # run. Next attempt then starts from a clean fresh-login.
            try:
                from .session_utils import invalidate_session_file
                invalidate_session_file("dice")
            except Exception:
                pass
            raise RuntimeError(
                "BLOCKED: Dice login did not complete (still on a login/challenge "
                f"page: url={page.url!r}). May require MFA, email code, or a captcha "
                "the solver could not pass."
            )
        logger.info(f"[Dice] Login OK (url={page.url!r})")

    async def _wait_logged_in(self, page: Page, timeout_s: float = 25.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            url = page.url.lower()
            if "/login" not in url and "/sign-in" not in url:
                # Off the login page — treat as authenticated.
                return True
            # Surface inline credential errors early.
            content = (await self._safe_content(page)).lower()
            if "incorrect" in content and "password" in content:
                logger.error("[Dice] Login rejected: incorrect email/password")
                return False
            await asyncio.sleep(1.0)
        return False

    async def _maybe_solve_captcha(self, page: Page) -> bool:
        """Detect a login captcha and try to solve it. Returns True if attempted."""
        try:
            has_recaptcha = await page.locator("iframe[src*='recaptcha']").count() > 0
            has_hcaptcha = await page.locator(
                "iframe[src*='hcaptcha'], .h-captcha[data-sitekey]"
            ).count() > 0
        except Exception:
            return False
        if not (has_recaptcha or has_hcaptcha):
            return False

        captcha_type = "hcaptcha" if has_hcaptcha else "recaptcha_v2"
        logger.info(f"[Dice] Login captcha detected ({captcha_type}); attempting solve")
        try:
            from ..captcha import CaptchaService
            provider = os.getenv("CAPTCHA_PROVIDER", "ai").lower()
            solution = await CaptchaService(provider=provider).solve(page, captcha_type)
            logger.info(f"[Dice] Captcha solve success={getattr(solution, 'success', False)}")
        except Exception as exc:
            logger.warning(f"[Dice] Captcha solve failed (non-fatal): {exc}")
        return True

    # ──────────────────────────────────────────────────────────────────────
    # Easy Apply detection
    # ──────────────────────────────────────────────────────────────────────

    async def _click_easy_apply(self, page: Page) -> bool:
        # The job id from /job-detail/<id> lets us target THIS job's wizard
        # anchor and ignore stray "Easy Apply" links on similar-job cards.
        m = re.search(r"/job-detail/([0-9a-f-]+)", page.url, re.IGNORECASE)
        job_id = m.group(1) if m else None

        # Candidate locators, BEST-FIRST. The href→wizard anchor is the most
        # reliable, label-independent signal (works whether the CTA reads
        # "Easy Apply" or "Continue Application").
        def _candidates():
            cands = []
            if job_id:
                cands.append(("href+jobid", page.locator(
                    f"a[href*='/job-applications/{job_id}/wizard']").first))
            cands.append(("href→wizard", page.locator(
                "a[href*='/job-applications/'][href*='wizard']").first))
            cands.append(("role=link[name]", page.get_by_role("link", name=_EASY_APPLY_NAME_RE).first))
            cands.append(("role=button[name]", page.get_by_role("button", name=_EASY_APPLY_NAME_RE).first))
            for sel in _EASY_APPLY_FALLBACK_SELECTORS:
                cands.append((sel, page.locator(sel).first))
            return cands

        # Dice renders the CTA via React a few seconds after load — poll, and
        # VERIFY the click actually navigated to the wizard before declaring
        # success (a stray "Easy Apply" link would click but not navigate).
        deadline = time.monotonic() + _EASY_APPLY_POLL_S
        while time.monotonic() < deadline:
            # If a previous click already navigated us into the wizard (the
            # click can detach the anchor mid-navigation and throw), we're done.
            if _WIZARD_URL_RE.search(page.url or ""):
                logger.info(f"[Dice] Wizard already reached (url={page.url!r})")
                return True
            for label, loc in _candidates():
                try:
                    if await loc.count() == 0 or not await loc.is_visible():
                        continue
                    await loc.scroll_into_view_if_needed()
                    await loc.click(timeout=_FIELD_TIMEOUT_MS)
                    # Confirm we actually entered the wizard.
                    try:
                        await page.wait_for_url(_WIZARD_URL_RE, timeout=8_000)
                        logger.info(f"[Dice] Easy Apply → wizard via {label}")
                        return True
                    except Exception:
                        if _WIZARD_URL_RE.search(page.url or ""):
                            logger.info(f"[Dice] Easy Apply → wizard via {label}")
                            return True
                        logger.debug(f"[Dice] {label} clicked but no wizard nav (url={page.url!r}); trying next")
                except Exception as exc:
                    logger.debug(f"[Dice] candidate {label} failed: {exc}")
            await asyncio.sleep(1.0)
        return False

    async def _has_external_apply(self, page: Page) -> bool:
        try:
            btn = page.get_by_role("button", name=_EXTERNAL_APPLY_NAME_RE).first
            if await btn.count() > 0 and await btn.is_visible():
                return True
        except Exception:
            pass
        try:
            return await page.locator("a:has-text('Apply now'), a:has-text('Apply on company')").count() > 0
        except Exception:
            return False

    # ──────────────────────────────────────────────────────────────────────
    # Submit / verify — fallback only (AgentLoop normally handles these).
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
        # Priority 1 — role=button with a submit-y name.
        try:
            btn = page.get_by_role("button", name=_SUBMIT_NAME_RE).first
            if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=_FIELD_TIMEOUT_MS)
                await self._settle_after_submit(page)
                logger.info("[Dice] Submitted via role=button[name~='submit']")
                return True
        except Exception as exc:
            logger.debug(f"[Dice] role-based submit failed: {exc}")

        for sel in _SUBMIT_FALLBACK_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=_FIELD_TIMEOUT_MS)
                await self._settle_after_submit(page)
                logger.info(f"[Dice] Submitted via {sel!r}")
                return True
            except Exception as exc:
                logger.debug(f"[Dice] submit selector {sel!r} failed: {exc}")
        logger.error("[Dice] No submit button matched")
        return False

    async def _settle_after_submit(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await self.human_delay(1.0, 2.0)

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        # Primary — success URL pattern.
        try:
            if _SUCCESS_URL_RE.search(page.url or ""):
                logger.info(f"[Dice] Success verified via URL {page.url!r}")
                return (True, "wizard/success")
        except Exception:
            pass
        # Secondary — page text.
        content = (await self._safe_content(page)).lower()
        for pattern in _SUCCESS_TEXT_PATTERNS:
            if pattern in content:
                logger.info(f"[Dice] Success verified via text {pattern!r}")
                return (True, pattern)
        return (False, None)

    # ──────────────────────────────────────────────────────────────────────
    # Low-level helpers
    # ──────────────────────────────────────────────────────────────────────

    async def _dismiss_overlays(self, page: Page) -> None:
        for sel in _COOKIE_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=2_000)
                    logger.info(f"[Dice] Dismissed overlay via {sel!r}")
                    await asyncio.sleep(0.3)
                    break
            except Exception:
                continue

    async def _email_field_visible(self, page: Page) -> bool:
        for sel in _EMAIL_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            except Exception:
                continue
        return False

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
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        return True
                except Exception:
                    continue
            await asyncio.sleep(0.4)
        return False

    @staticmethod
    async def _safe_content(page: Page) -> str:
        try:
            return await page.content()
        except Exception:
            return ""

    @staticmethod
    def _page_text_has(content: str, patterns) -> bool:
        low = (content or "").lower()
        return any(p in low for p in patterns)
