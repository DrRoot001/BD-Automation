"""Talent.com native Easy Apply adapter.

Talent.com's Easy Apply is NOT account-walled — instead it gates the
application behind an emailed OTP code (email -> "check your email" -> 6-box
code -> contact info -> review -> send). This adapter owns only the
deterministic, non-form-fill concerns, exactly per the operator rule
(*new adapters defer to the AgentLoop, expose knowledge as hints, never
script form-fill*):

  1. **Navigation** — open the job page and click the "Apply Now" / "Easy
     Apply" CTA. The CTA redirects into the apply flow, which may briefly
     render a near-blank page with a captcha while it loads.
  2. **Captcha interstitial** — best-effort solve of the post-Apply captcha
     (reCAPTCHA / hCaptcha via CaptchaService; the AI-vision solver runs
     first and needs no paid key). Cloudflare Turnstile is detected and
     logged but cannot be token-solved — it usually passes on a
     stealth-configured session after a short wait.

Everything after that — email entry, OTP (auto-fetched + filled by the
AgentLoop's mid-flow verification handler), Step 1 contact info + resume
upload, Step 2 review, and the final "Send application" submit — is driven
by the AgentLoop using the rich ``talent`` entry in ``adapters/hints.py``.
``submit`` / ``verify_success`` here are deterministic safety-net fallbacks
only.

URL note (per spec — NEVER rely on ids / query params): job pages live on
www.talent.com (e.g. /jobs?k=...&id=<id>). Validate pages by heading / step
text, not URL.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional, Tuple

from playwright.async_api import Page

from .base import BasePlatformAdapter

logger = logging.getLogger(__name__)

_NAV_TIMEOUT_MS = 40_000
_FIELD_TIMEOUT_MS = 8_000
_APPLY_POLL_S = 20.0

# Apply CTA. Talent.com's /jobs page is a TWO-PANE search UI: a job list on
# the left and the SELECTED job's detail on the right. The detail pane's apply
# CTA is an <a target=_blank> whose href is
# "/redirect?id=<jobid>&pid=...&action=quickapply" — that href (NOT the visible
# text) is the reliable, label-independent signal. CRUCIAL: there is ALSO a
# "Quick Apply" FILTER chip (a <button> with NO href) in the top filter bar;
# matching on text alone clicks the filter and toggles apply_type=quickApply
# instead of applying. So _read_apply_href() targets the href only.

# Cookie / consent banners commonly seen on talent.com.
_COOKIE_SELECTORS = (
    "button#onetrust-accept-btn-handler",
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept')",
    "button:has-text('I agree')",
    "button:has-text('Got it')",
)

# Final submit on the Review step (fallback only — AgentLoop normally submits).
_SUBMIT_NAME_RE = re.compile(r"send application|submit application|^submit$", re.IGNORECASE)
_SUBMIT_FALLBACK_SELECTORS = (
    "button:has-text('Send application')",
    "button:has-text('Send Application')",
    "button:has-text('Submit application')",
    "button:has-text('Submit')",
    "button[type='submit']",
)

_SUCCESS_TEXT_PATTERNS = (
    "application submitted",
    "application sent",
    "your application has been sent",
    "we've received your application",
    "successfully applied",
    "thank you for applying",
    "thank you",
)


class TalentAdapter(BasePlatformAdapter):
    platform_name = "talent"
    # Apply flow is a plain (non-iframe) SPA. Left unscoped so the scripted
    # fallback can scan the whole page; the AgentLoop does its own scoping.
    container_selector = None

    def __init__(self) -> None:
        # Mirror the iframe attributes other adapters expose so executor
        # getattr() reads return None cleanly (Talent has no form iframe).
        self._iframe_mode: bool = False
        self._frame_locator = None
        self._frame = None
        # Set by the executor before navigate() so the adapter can fill the
        # email gate deterministically (avoids the AI mis-clicking the
        # "Continue with Google" SSO button). Optional — falls back to the
        # AgentLoop if absent.
        self.candidate_profile: Optional[dict] = None

    # ──────────────────────────────────────────────────────────────────────
    # Navigation (open job -> click Apply -> handle captcha interstitial)
    # ──────────────────────────────────────────────────────────────────────

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        logger.info(f"[Talent] Navigating to job {job_url!r}")
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        except Exception as exc:
            logger.warning(f"[Talent] job goto soft-failed ({exc}); settling page")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8_000)
            except Exception:
                pass

        await self._dismiss_overlays(page)
        await self.human_delay(0.8, 1.6)

        # The /jobs page is a two-pane search UI. The selected job's apply CTA
        # is an <a target=_blank href="/redirect?id=<id>&pid=...&action=quickapply">.
        # Clicking it opens a NEW TAB; instead we read the absolute href and
        # navigate the SAME tab to it, so the executor's single `page` stays on
        # the apply flow (no cross-tab page-swap needed downstream).
        apply_href = await self._read_apply_href(page)
        if not apply_href:
            raise RuntimeError(
                "BLOCKED: Talent.com Quick Apply link not found. Posting may be "
                "expired, region-gated, not Quick-Apply, or the DOM shifted."
            )
        logger.info(f"[Talent] Apply href resolved: {apply_href}")
        try:
            await page.goto(apply_href, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        except Exception as exc:
            logger.warning(f"[Talent] apply-href goto soft-failed ({exc}); continuing")

        # The redirect surface is a near-blank page guarding the apply flow with
        # a Google reCAPTCHA v2 checkbox. Solve it (free AI checkbox-pass +
        # Whisper audio), then wait for the email/sign-in page to render.
        await self._solve_interstitial_captcha(page)
        await self._wait_for_apply_surface(page)

        # Deterministically clear the email gate (fill email + click the EXACT
        # "Continue" — NOT "Continue with Google") so the OTP is sent. The
        # AgentLoop's mid-flow handler then auto-fetches + fills the code, and
        # the AI drives the contact/review/submit steps. This is infra (like a
        # login), so the AI never has to disambiguate the SSO button.
        await self._enter_email_gate(page)
        await self.human_delay(0.6, 1.2)

    async def detect_application_type(self, page: Page) -> str:
        return "EASY_APPLY"

    async def refresh_frame(self, page: Page) -> None:
        return None

    # ──────────────────────────────────────────────────────────────────────
    # Apply CTA
    # ──────────────────────────────────────────────────────────────────────

    async def _read_apply_href(self, page: Page) -> Optional[str]:
        """Poll for the selected job's Quick-Apply anchor and return its
        ABSOLUTE href. The href (not the visible text) is the reliable signal:
        the "Quick Apply" filter chip in the top bar is a <button> with no href
        and so is never matched here. We prefer the anchor whose href carries
        THIS job's id."""
        m = re.search(r"[?&]id=(\d+)", page.url)
        job_id = m.group(1) if m else None
        deadline = asyncio.get_event_loop().time() + _APPLY_POLL_S
        while asyncio.get_event_loop().time() < deadline:
            href = await page.evaluate(
                """(jobId) => {
                    const anchors = Array.from(
                        document.querySelectorAll("a[href*='quickapply'], a[href*='action=quickapply']")
                    );
                    if (!anchors.length) return null;
                    if (jobId) {
                        const m = anchors.find(a => (a.getAttribute('href')||'').includes('id=' + jobId));
                        if (m) return m.href;  // .href is absolute
                    }
                    return anchors[0].href;
                }""",
                job_id,
            )
            if href:
                return href
            await asyncio.sleep(0.8)
        return None

    # ──────────────────────────────────────────────────────────────────────
    # Captcha interstitial (reCAPTCHA v2 on the /redirect page)
    # ──────────────────────────────────────────────────────────────────────

    async def _wait_for_recaptcha(self, page: Page, timeout_s: float = 25.0) -> bool:
        """Wait until the reCAPTCHA anchor iframe + checkbox are actually
        present. CRITICAL: the widget loads a few seconds AFTER the redirect
        page, so solving immediately (before this) silently fails — the
        original observed failure mode."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            for fr in page.frames:
                src = fr.url or ""
                if "recaptcha" in src and "anchor" in src:
                    try:
                        if await fr.locator("#recaptcha-anchor").count() > 0:
                            return True
                    except Exception:
                        pass
            await asyncio.sleep(0.5)
        return False

    async def _solve_interstitial_captcha(self, page: Page) -> bool:
        """Solve the reCAPTCHA v2 guarding the apply flow. Returns True if a
        solve attempt was made. Non-fatal — if the widget never appears we
        assume the session was trusted and skip straight to the form."""
        # If we already landed on the apply/email surface, no captcha to solve.
        if "/apply" in (page.url or ""):
            return False

        if not await self._wait_for_recaptcha(page, timeout_s=25.0):
            # No reCAPTCHA — also check hCaptcha/Turnstile just in case the
            # platform changes its bot wall.
            try:
                has_h = await page.locator("iframe[src*='hcaptcha'], .h-captcha").count() > 0
            except Exception:
                has_h = False
            if not has_h:
                logger.info("[Talent] No captcha widget appeared on the apply surface")
                return False
            captcha_type = "hcaptcha"
        else:
            captcha_type = "recaptcha_v2"

        logger.info(f"[Talent] Captcha detected ({captcha_type}); solving (AI + Whisper audio)")
        from ..captcha import CaptchaService
        provider = os.getenv("CAPTCHA_PROVIDER", "ai").lower()
        # reCAPTCHA v2 free-solving (AI checkbox-pass + Whisper audio) is
        # probabilistic — retry a few times to maximize the FIRST-pass success.
        # Once passed, the session cookies are trusted and subsequent runs skip
        # the captcha entirely (talent only challenges fresh/untrusted sessions).
        max_solve = int(os.getenv("TALENT_CAPTCHA_ATTEMPTS", "3"))
        for attempt in range(1, max_solve + 1):
            try:
                solution = await CaptchaService(provider=provider).solve(page, captcha_type)
                ok = bool(getattr(solution, "success", False))
                logger.info(f"[Talent] Captcha solve attempt {attempt}/{max_solve} success={ok}")
            except Exception as exc:
                logger.warning(f"[Talent] Captcha solve raised (non-fatal): {exc}")
                ok = False
            # Whether or not the solver reports success, the page may have
            # advanced (Google commits the token to the widget directly).
            await asyncio.sleep(3.0)
            if "/apply" in (page.url or "") or await self._email_surface_visible(page):
                logger.info("[Talent] Apply surface reached after captcha")
                return True
            # Re-wait for a fresh widget before the next attempt.
            if attempt < max_solve:
                await self._wait_for_recaptcha(page, timeout_s=10.0)
        logger.warning(
            "[Talent] reCAPTCHA not passed after retries. This session stayed "
            "untrusted; a funded 2captcha/anticaptcha key or a pre-seeded "
            "trusted session is the reliable path for the first pass."
        )
        return True

    # Any of these means the apply flow has actually RENDERED (not a blank /
    # transitional page): the email gate, the OTP boxes, or the contact form.
    _APPLY_FIELDS = (
        "input[type='email'], input[name*='email' i], input[id*='email' i], "
        "input[label='First name'], input[id*='first' i], "
        "#phone-input, #resume-upload, input[autocomplete='one-time-code']"
    )

    async def _email_surface_visible(self, page: Page) -> bool:
        try:
            return await page.locator(self._APPLY_FIELDS).count() > 0
        except Exception:
            return False

    async def _wait_for_apply_surface(self, page: Page, timeout_s: float = 30.0) -> None:
        """After the captcha, wait for the apply flow to actually RENDER (email
        gate / OTP / contact form fields) so the AgentLoop starts on a real
        page rather than the transient near-blank redirect page. Waiting on
        actual fields — not just the URL — is what stops the AI from 'struggling
        to see the page' on its first turns."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            if "/apply" in (page.url or "") and await self._email_surface_visible(page):
                await self._dismiss_overlays(page)
                # Let the SPA finish painting before the AgentLoop's first frame.
                await asyncio.sleep(1.2)
                return
            await asyncio.sleep(0.6)
        logger.warning(
            f"[Talent] Apply surface fields not confirmed within {timeout_s:.0f}s "
            f"(url={page.url!r}); handing to AgentLoop anyway"
        )

    # ──────────────────────────────────────────────────────────────────────
    # Email gate (deterministic — triggers the OTP)
    # ──────────────────────────────────────────────────────────────────────

    async def _enter_email_gate(self, page: Page) -> None:
        """Fill the 'Sign in to apply' email field and click the exact
        'Continue' (NOT 'Continue with Google'), which emails the OTP. No-op if
        the email field isn't visible (e.g. an existing session already moved us
        to the contact form) or no candidate email is available."""
        email = ((self.candidate_profile or {}).get("email") or "").strip()
        if not email:
            logger.info("[Talent] No candidate email on adapter — leaving email gate to AgentLoop")
            return

        # If a first-name field (or the OTP boxes) is already present, we're on
        # the contact/OTP step (logged-in/returning user) — the email gate is
        # already cleared. Talent uses a `label=` attribute + hashed names, so
        # match on that, not name/id.
        try:
            if await page.locator(
                "input[label='First name'], input[id*='first' i], "
                "input[autocomplete='one-time-code']"
            ).count() > 0:
                logger.info("[Talent] Contact/OTP step already present — email gate cleared; skipping")
                return
        except Exception:
            pass

        email_sel = "input[type='email'], input[name*='email' i], input[id*='email' i]"
        loc = page.locator(email_sel).first
        try:
            # Skip if the email field is absent, hidden, OR disabled (a disabled
            # prefilled email means we're already authenticated past the gate).
            if (await loc.count() == 0 or not await loc.is_visible()
                    or not await loc.is_enabled()):
                logger.info("[Talent] No editable email input — past the gate already; skipping")
                return
            await loc.scroll_into_view_if_needed()
            await loc.click(timeout=_FIELD_TIMEOUT_MS)
            await loc.fill("")
            await loc.fill(email)
            logger.info(f"[Talent] Email gate filled with {email!r}")
        except Exception as exc:
            logger.warning(f"[Talent] email gate fill failed (non-fatal): {exc}")
            return

        await self.human_delay(0.4, 0.9)

        clicked = False
        try:
            btn = page.get_by_role("button", name="Continue", exact=True).first
            if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                await btn.click(timeout=_FIELD_TIMEOUT_MS)
                clicked = True
        except Exception as exc:
            logger.debug(f"[Talent] exact-name Continue click failed: {exc}")
        if not clicked:
            for sel in (
                "button:text-is('Continue')",
                "button:has-text('Continue'):not(:has-text('Google'))",
            ):
                try:
                    b = page.locator(sel).first
                    if await b.count() > 0 and await b.is_visible() and await b.is_enabled():
                        await b.click(timeout=_FIELD_TIMEOUT_MS)
                        clicked = True
                        break
                except Exception:
                    continue
        if not clicked:
            # Last resort — submit the email by pressing Enter in the field.
            try:
                await loc.press("Enter")
                clicked = True
            except Exception:
                pass

        if clicked:
            logger.info("[Talent] Email gate submitted — OTP should be emailed shortly")
            await asyncio.sleep(3.0)
        else:
            logger.warning("[Talent] Could not submit email gate — AgentLoop will attempt it")

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
        try:
            btn = page.get_by_role("button", name=_SUBMIT_NAME_RE).first
            if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=_FIELD_TIMEOUT_MS)
                await self._settle_after_submit(page)
                logger.info("[Talent] Submitted via role=button[name~='send application']")
                return True
        except Exception as exc:
            logger.debug(f"[Talent] role-based submit failed: {exc}")

        for sel in _SUBMIT_FALLBACK_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=_FIELD_TIMEOUT_MS)
                await self._settle_after_submit(page)
                logger.info(f"[Talent] Submitted via {sel!r}")
                return True
            except Exception as exc:
                logger.debug(f"[Talent] submit selector {sel!r} failed: {exc}")
        logger.error("[Talent] No submit button matched")
        return False

    async def _settle_after_submit(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await self.human_delay(1.0, 2.0)

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        content = (await self._safe_content(page)).lower()
        for pattern in _SUCCESS_TEXT_PATTERNS:
            if pattern in content:
                logger.info(f"[Talent] Success verified via text {pattern!r}")
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
                    logger.info(f"[Talent] Dismissed overlay via {sel!r}")
                    await asyncio.sleep(0.3)
                    break
            except Exception:
                continue

    @staticmethod
    async def _safe_content(page: Page) -> str:
        try:
            return await page.content()
        except Exception:
            return ""
