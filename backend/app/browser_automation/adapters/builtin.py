"""Built In (builtin.com) adapter — click-through job-board resolver.

Empirical reality (not what the operator spec described):
    builtin.com/job/<title>/<id>  is NOT a native ATS. It's a job-board
    listing that shows a 3-field mini-form (first name / last name /
    email) inline when the user clicks Apply. After filling that form
    and clicking Continue, Built In OPENS A NEW BROWSER TAB pointing at
    the underlying ATS (iCIMS / Greenhouse / Lever / Workable / etc.)
    — with a tracking referrer (iisn=BuiltIn, iis=Job+Board+Paid).

    So Built In is architecturally identical to RemoteRocketship's
    click-through model: it's a wrapper. The pattern here mirrors
    RemoteRocketshipAdapter (see adapters/remoterocketship.py) — do the
    mini-form dance, capture the ATS URL from the new tab, then delegate
    every subsequent adapter method to the target ATS's adapter.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .base import BasePlatformAdapter
from .session_utils import invalidate_session_file, session_file_path

logger = logging.getLogger(__name__)

# builtin.com/login round-trips through accounts.builtin.com (OIDC). With a
# live session cookie it bounces straight back authenticated; logged-out it
# renders the email screen ("Enter your email and we'll send you a one-time
# link"). There is NO password login — the only alternatives are Google SSO
# (forbidden by operator policy) and the passwordless email link used here.
_LOGIN_URL = "https://builtin.com/login"
_EMAIL_FIELD = "input#Email, input[name='Email']"
_LOGIN_SUBMIT = "button:has-text('LOGIN'), button[type='submit']"


def _newtab_timeout_ms() -> int:
    """How long to wait for Built In's Continue click to open the ATS tab.
    `BUILTIN_NEWTAB_TIMEOUT_MS` overrides the default; bumped 15s → 25s because
    a slow employer ATS can miss a 15s window and fall back to generic."""
    try:
        return max(5000, int(os.getenv("BUILTIN_NEWTAB_TIMEOUT_MS", "25000")))
    except (TypeError, ValueError):
        return 25000


# Known ATS host → registry key. Same shape as remoterocketship's map, but
# tailored to what Built In actually hands off to in practice. Order
# matters for prefix substring fallback.
_ATS_HOSTS: tuple[tuple[str, str], ...] = (
    ("job-boards.greenhouse.io",  "greenhouse"),
    ("boards.greenhouse.io",       "greenhouse"),
    ("greenhouse.io",              "greenhouse"),
    ("jobs.lever.co",              "lever"),
    ("lever.co",                   "lever"),
    ("jobs.ashbyhq.com",           "ashby"),
    ("ashbyhq.com",                "ashby"),
    ("myworkdayjobs.com",          "workday"),
    ("workday.com",                "workday"),
    ("icims.com",                  "icims"),
    ("dice.com",                   "dice"),
    ("indeed.com",                 "indeed"),
    ("linkedin.com",               "linkedin"),
)


def _detect_ats_from_url(url: str) -> Optional[str]:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    for needle, key in _ATS_HOSTS:
        if needle in host:
            return key
    return None


class BuiltInAdapter(BasePlatformAdapter):
    platform_name = "builtin"
    container_selector = None

    def __init__(self) -> None:
        self._inner: Optional[BasePlatformAdapter] = None
        self._resolved_url: Optional[str] = None
        # Executor reads these via getattr — mirror from the inner adapter.
        self._iframe_mode: bool = False
        self._frame_locator = None
        self._frame = None

    # ──────────────────────────────────────────────────────────────────────
    # Candidate login (passwordless one-time email link — never Google SSO)
    # ──────────────────────────────────────────────────────────────────────

    async def _on_login_screen(self, page: Page) -> bool:
        try:
            if "accounts.builtin.com" not in (page.url or "").lower():
                return False
            return await page.locator(_EMAIL_FIELD).first.is_visible()
        except Exception:
            return False

    async def _submit_login_email(self, page: Page, email: str) -> bool:
        """Fill the email field and click LOGIN. Recovers from the error page
        accounts.builtin.com intermittently serves by reloading /login first."""
        try:
            url = (page.url or "").lower()
            if "accounts.builtin.com" not in url or url.startswith("chrome-error"):
                await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(3.0)
            box = page.locator(_EMAIL_FIELD).first
            await box.wait_for(state="visible", timeout=15_000)
            await box.fill(email)
            await self.human_delay(0.3, 0.9)
            await page.locator(_LOGIN_SUBMIT).first.click(timeout=5_000)
            logger.info(f"[BuiltIn] one-time-link request submitted for {email!r}")
            await asyncio.sleep(4.0)
            return True
        except Exception as exc:
            logger.warning(f"[BuiltIn] login email submit failed: {exc}")
            return False

    async def _ensure_logged_in(self, page: Page) -> bool:
        """Authenticate on builtin.com AS THE CANDIDATE via the passwordless
        email one-time-link flow (builtin has no password login; Google SSO is
        deliberately never used).

        Returns True when a logged-in session exists (restored or fresh),
        False when no credentials/candidate identity is available (caller
        falls back to the legacy anonymous mini-form flow). Raises
        RuntimeError("BLOCKED: ...") when the login email was submitted but
        the one-time link never arrived — the account-walled apply is doomed
        without it, and BLOCKED routes to the standard login-failure handling.
        """
        email = self._login_credential(
            "login_email", "BUILTIN_EMAIL", "BUILTIN_CANDIDATE_EMAIL"
        )
        candidate_id = getattr(self, "candidate_id", None)
        if not email or not candidate_id:
            logger.info(
                "[BuiltIn] no login email / candidate_id available — "
                "proceeding with the anonymous flow"
            )
            return False

        # Restore a previously-saved session directly onto this context.
        # context_manager's file-based restore only engages when the executor's
        # platform key is literally 'builtin'; ad-hoc URL runs pass the full
        # job URL as the platform, so restore here too (idempotent either way).
        try:
            import json as _json
            _sess = session_file_path("builtin")
            if _sess.is_file():
                _cookies = (_json.loads(_sess.read_text()) or {}).get("cookies") or []
                if _cookies:
                    await page.context.add_cookies(_cookies)
                    logger.info(
                        f"[BuiltIn] restored {len(_cookies)} session cookies ← {_sess}"
                    )
        except Exception as exc:
            logger.debug(f"[BuiltIn] session cookie restore skipped: {exc}")

        try:
            await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.warning(f"[BuiltIn] login page goto failed: {exc}")
            return False

        # builtin.com/login renders a "Redirecting…" shim for several seconds
        # before bouncing to accounts.builtin.com (logged out) or back into
        # builtin.com (session live). Staying on /login means NOTHING has been
        # decided yet — wait for a decisive state, never infer from silence.
        on_wall = False
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            u = (page.url or "").lower()
            if "accounts.builtin.com" in u and await self._on_login_screen(page):
                on_wall = True
                break
            if ("builtin.com" in u and "accounts.builtin.com" not in u
                    and "/login" not in u):
                # OIDC bounced us straight back — restored session is live.
                logger.info(f"[BuiltIn] session already authenticated (url={page.url!r})")
                return True
            await asyncio.sleep(1.0)

        # Reaching here means the restored session did NOT authenticate us — we
        # either landed on the login wall or the state stayed undecided (a live
        # session returns True above). The cookies context_manager replayed are
        # stale; wipe them (Redis blob + disk file) so the next run starts clean
        # instead of re-replaying dead cookies into accounts.builtin.com, which
        # 500s on stale OIDC state. On the wall path we re-login + re-save below.
        try:
            from ..browser.context_manager import clear_redis_session
            await clear_redis_session(candidate_id, "builtin.com")
        except Exception as exc:
            logger.debug(f"[BuiltIn] redis session clear skipped: {exc}")
        invalidate_session_file("builtin")

        if not on_wall:
            logger.warning(
                f"[BuiltIn] login state undecided after 30s (url={page.url!r}) "
                "— proceeding anonymously"
            )
            return False

        from ..verification.code_fetcher import fetch_magic_link

        magic = None
        for attempt in (1, 2):
            # Epoch marker BEFORE the send so the Gmail poll only accepts a
            # link generated by THIS submission (5s skew tolerance).
            after_epoch = int(time.time()) - 5
            await self._submit_login_email(page, email)
            # Poll Gmail even if the response page errored — accounts.builtin
            # sometimes 500s AFTER dispatching the email.
            # BuiltIn's SES delivery is slow — observed 2.5–5 min from submit
            # to inbox. Short windows lose the race (a link once arrived 2s
            # AFTER a 150s poll gave up).
            magic = await fetch_magic_link(
                candidate_id,
                after_epoch,
                sender_hint="builtin",
                host_hint="builtin.com",
                timeout_s=360.0 if attempt == 1 else 240.0,
            )
            if magic:
                break
            logger.warning(
                f"[BuiltIn] one-time link not received (attempt {attempt}/2)"
            )

        if not magic:
            raise RuntimeError(
                "BLOCKED: BuiltIn one-time login link never arrived in the "
                f"candidate's Gmail ({email}). Passwordless login is the only "
                "non-SSO path — check the account exists on builtin.com, the "
                "google_refresh_token is valid, and the message isn't in spam."
            )

        logger.info("[BuiltIn] one-time login link received — opening")
        try:
            await page.goto(magic, wait_until="domcontentloaded", timeout=45_000)
        except Exception as exc:
            logger.warning(f"[BuiltIn] magic-link navigation error: {exc}")
        # accounts.builtin.com shows an auto-running "Verifying login…" page
        # (/Token/Verify) and then round-trips OIDC back into builtin.com.
        # Verification appears to be bound to the browser context that
        # requested the link (a fresh context stalls on /Token/Verify forever)
        # — this page IS that context, but give it a generous window.
        verified = False
        for _ in range(60):
            u = (page.url or "").lower()
            if ("builtin.com" in u and "accounts.builtin.com" not in u
                    and not u.startswith("chrome-error")):
                verified = True
                break
            await asyncio.sleep(1.0)
        if not verified:
            raise RuntimeError(
                "BLOCKED: BuiltIn one-time login link did not complete "
                f"verification (stuck at {page.url!r}) — token expired/"
                "consumed, or Token/Verify rejected this browser context."
            )

        # Persist the session so every future run (context_manager auto-loads
        # backend/data/sessions/builtin.json) skips this entire dance.
        try:
            path = session_file_path("builtin")
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.context.storage_state(path=str(path))
            logger.info(f"[BuiltIn] session persisted → {path}")
        except Exception as exc:
            logger.warning(f"[BuiltIn] session persist failed (non-fatal): {exc}")

        logger.info(f"[BuiltIn] logged in as {email!r} (url={page.url!r})")
        return True

    # ──────────────────────────────────────────────────────────────────────
    # Click-through resolution
    # ──────────────────────────────────────────────────────────────────────

    async def _click_through_to_ats(
        self, page: Page, job_url: str, candidate_email: Optional[str] = None
    ) -> Optional[str]:
        """Perform Built In's mini-form dance and return the ATS URL that
        opens in the new tab, or None if it never opens.

        Steps:
          1. Load the Built In job page (no Apply visible yet).
          2. Click the first Apply-like button — this reveals the inline
             3-field mini-form (#first-name / #last-name / #user-email).
          3. Fill first name / last name / email with the candidate's
             identity data (or generic dummies — Built In doesn't
             validate; the real form is on the ATS side).
          4. Register a page-opened listener on the browser context
             BEFORE clicking Continue — Built In opens the ATS in a
             new tab, which we need to catch synchronously.
          5. Click Continue. Await the new-tab event with a 15s window.
          6. Return the new tab's URL, and switch the executor's active
             page to that tab so the delegated ATS adapter drives it.
        """
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=25_000)
        except Exception as exc:
            logger.warning(f"[BuiltIn] initial goto failed: {exc}")

        await asyncio.sleep(2.0)

        # 2. Click the visible Apply link/button on the job listing.
        apply_selectors = [
            "a:has-text('Apply')",
            "button:has-text('Apply')",
            "button:has-text('Easy Apply')",
            "a:has-text('Easy Apply')",
            "[data-testid*='apply']",
        ]
        clicked = False
        for sel in apply_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.scroll_into_view_if_needed()
                    await loc.click(timeout=5_000)
                    logger.info(f"[BuiltIn] Apply clicked via {sel!r}")
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            logger.warning("[BuiltIn] no Apply button found — cannot resolve ATS URL")
            return None

        # Give the inline mini-form time to render.
        await asyncio.sleep(1.5)

        # 3. Fill first/last/email — Built In doesn't validate strictly here;
        # the real form is on the ATS side. Use candidate identity if we
        # have it, else safe placeholders.
        try:
            fn = page.locator("input#first-name, input[name='first-name']").first
            if await fn.count() > 0 and await fn.is_visible():
                await fn.fill(os.getenv("BUILTIN_FIRST_NAME", "Candidate"))
            ln = page.locator("input#last-name, input[name='last-name']").first
            if await ln.count() > 0 and await ln.is_visible():
                await ln.fill(os.getenv("BUILTIN_LAST_NAME", "Applicant"))
            em = page.locator("input#user-email, input[name='email']").first
            if await em.count() > 0 and await em.is_visible():
                await em.fill(candidate_email or os.getenv("BUILTIN_CANDIDATE_EMAIL", "candidate@example.com"))
        except Exception as exc:
            logger.warning(f"[BuiltIn] mini-form fill error (non-fatal): {exc}")

        # 4. Arm the new-page listener BEFORE clicking Continue.
        ctx = page.context
        _newtab_ms = _newtab_timeout_ms()
        new_page_future = asyncio.ensure_future(
            ctx.wait_for_event("page", timeout=_newtab_ms)
        )

        # 5. Click Continue.
        try:
            cont = page.locator(
                "button:has-text('Continue'), a:has-text('Continue')"
            ).first
            if await cont.count() > 0 and await cont.is_visible():
                await cont.click(timeout=5_000)
                logger.info("[BuiltIn] Continue clicked — waiting for ATS tab.")
            else:
                logger.warning("[BuiltIn] Continue button not found")
                new_page_future.cancel()
                return None
        except Exception as exc:
            logger.warning(f"[BuiltIn] Continue click failed: {exc}")
            new_page_future.cancel()
            return None

        # 6. Wait for the new tab to open.
        try:
            new_page = await new_page_future
        except (asyncio.TimeoutError, PlaywrightTimeoutError, asyncio.CancelledError):
            logger.warning(
                f"[BuiltIn] no new tab opened within {_newtab_ms // 1000}s — Built In may "
                "have changed its flow, or Continue didn't dispatch the handoff."
            )
            return None

        try:
            await new_page.wait_for_load_state(
                "domcontentloaded", timeout=15_000
            )
        except Exception:
            pass
        resolved_url = new_page.url
        logger.info(f"[BuiltIn] resolved ATS URL: {resolved_url!r}")

        # Bring the ATS tab to the front so the rest of the pipeline can
        # observe it visually + drive it.
        try:
            await new_page.bring_to_front()
        except Exception:
            pass

        # Swap the executor's active page reference so subsequent adapter
        # methods drive the NEW tab (not the Built In job-listing tab).
        # We do this by storing the new page on self and having the
        # inner adapter's method calls use it. The executor holds `page`
        # externally though — trickiest bit — so also close the Built In
        # tab and hope Playwright falls back to the new one. Actually,
        # cleaner: swap the reference the adapter sees, then re-invoke
        # the inner adapter's navigate on THAT page.
        self._new_tab_page = new_page
        return resolved_url

    # ──────────────────────────────────────────────────────────────────────
    # BasePlatformAdapter — all methods delegate to the inner ATS adapter
    # once resolved. Before resolution: fall through to a minimal no-op
    # so the AgentLoop's own vision agent can salvage weird pages.
    # ──────────────────────────────────────────────────────────────────────

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        # Lazy import to avoid circular imports with adapters/__init__.
        from .registry import get_adapter

        # 0. If the URL we were handed is ALREADY a known ATS (i.e. the
        # jobs.source_url in the DB is the resolved iCIMS/Greenhouse
        # URL and jobs.source is 'builtin' just as a label), skip the
        # mini-form dance and delegate straight to that ATS adapter —
        # mirrors RemoteRocketshipAdapter.navigate_to_application().
        direct_key = _detect_ats_from_url(job_url)
        hostname = (urlparse(job_url).hostname or "").lower()
        if direct_key and "builtin" not in hostname:
            self._inner = self._spawn_delegate(direct_key)
            self._resolved_url = job_url
            logger.info(
                f"[BuiltIn] URL is already a resolved {direct_key!r} ATS — "
                "skipping mini-form, delegating directly."
            )
            await self._inner.navigate_to_application(page, job_url)
            self._mirror_inner_attrs()
            return

        # 0.5. builtin.com now walls Easy Apply behind an account: logged out,
        # the Apply click redirects to accounts.builtin.com (Google SSO or
        # one-time email link — no password exists). Log in as the candidate
        # first via the email-link flow; anonymous fallback keeps the legacy
        # mini-form flow working for jobs that still allow it. A BLOCKED
        # RuntimeError from a failed credential login propagates — an
        # account-walled job cannot proceed logged-out.
        await self._ensure_logged_in(page)

        # 1. Do the mini-form + Continue + new-tab dance on Built In.
        resolved = await self._click_through_to_ats(page, job_url)

        if not resolved:
            # No handoff detected — fall through with a generic adapter
            # so the AgentLoop's vision agent can try to salvage the
            # page. Won't succeed in most cases but avoids a hard abort.
            logger.warning(
                f"[BuiltIn] click-through resolution failed for {job_url!r}; "
                "falling through to generic vision agent."
            )
            self._inner = self._spawn_delegate("generic")
            self._mirror_inner_attrs()
            return

        ats_key = _detect_ats_from_url(resolved) or "generic"
        self._inner = self._spawn_delegate(ats_key)
        self._resolved_url = resolved
        logger.info(
            f"[BuiltIn] Delegating to {ats_key!r} adapter for {resolved!r}"
        )

        # The executor holds a reference to the ORIGINAL page (the Built
        # In listing tab). We need subsequent adapter calls to operate on
        # the NEW tab. Two options:
        #   (a) swap Playwright's own tab so the original page reference
        #       transparently now targets the new tab — not possible.
        #   (b) navigate the ORIGINAL page directly to the resolved URL —
        #       this replaces its content with the ATS form, matching
        #       what remoterocketship.py does. The new tab that Built In
        #       opened is closed afterward as noise.
        # (b) is what mirrors RR's pattern and works with the existing
        # executor flow. Do that.
        try:
            new_tab = getattr(self, "_new_tab_page", None)
            if new_tab is not None:
                try:
                    await new_tab.close()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            await page.goto(resolved, wait_until="domcontentloaded", timeout=25_000)
        except Exception as exc:
            logger.warning(
                f"[BuiltIn] goto resolved URL failed: {exc}; inner adapter "
                "will still attempt its own navigate."
            )

        await self._inner.navigate_to_application(page, resolved)
        self._mirror_inner_attrs()

    def _mirror_inner_attrs(self) -> None:
        if not self._inner:
            return
        self._iframe_mode = getattr(self._inner, "_iframe_mode", False)
        self._frame_locator = getattr(self._inner, "_frame_locator", None)
        self._frame = getattr(self._inner, "_frame", None)

    async def detect_application_type(self, page: Page) -> str:
        if self._inner:
            try:
                return await self._inner.detect_application_type(page)
            except NotImplementedError:
                pass
        return "EXTERNAL_FORM"

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
        if not self._inner:
            return False
        return await self._inner.fill_application(
            page, profile, resume_path, cover_letter_path,
            screening_answers, pre_detected_form=pre_detected_form,
            candidate_id=candidate_id,
        )

    async def submit(self, page: Page) -> bool:
        if not self._inner:
            return False
        return await self._inner.submit(page)

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        if not self._inner:
            return (False, None)
        return await self._inner.verify_success(page)

    async def refresh_frame(self, page: Page) -> None:
        if self._inner and hasattr(self._inner, "refresh_frame"):
            await self._inner.refresh_frame(page)
            self._mirror_inner_attrs()
