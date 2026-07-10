"""Indeed adapter — branch classifier + passthrough router.

Indeed has TWO application branches off every job-detail page:

  1. EASY_APPLY — "Apply with Indeed" / "Easily apply". The form lives at
     smartapply.indeed.com. The AgentLoop drives the multi-step wizard
     (location → resume → self-ID → review → submit) using the `smartapply`
     hints in hints.py. No scripted form-fill here (operator standing rule:
     AI master, Playwright slave).

  2. EXTERNAL_APPLY — "Apply on company site" / "Apply now". Clicking opens
     a new tab OR redirects to a non-indeed.com employer/ATS host. We resolve
     that host, instantiate the matching ATS adapter, and delegate everything
     to it (same pattern as RemoteRocketshipAdapter).

This adapter is intentionally thin. It owns:
  - Loading the Indeed job page with bot-wall + expired-listing detection.
  - Classifying the apply button.
  - Following the click (popup OR same-tab redirect) for the external branch.
  - Delegating to the inner adapter for everything past navigation.

It does NOT own form-filling logic for either branch — that's the AgentLoop's
job, with platform-specific knowledge injected via hints.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import Page

from .base import BasePlatformAdapter

logger = logging.getLogger(__name__)

_NAV_TIMEOUT_MS = 25_000
_POPUP_WAIT_MS = 8_000
_REDIRECT_WAIT_MS = 12_000

# Branch labels (mirrored from SKILLS.md state names — kept as bare strings,
# not an enum, so the AgentLoop can echo them in its reasoning verbatim).
BRANCH_EASY_APPLY = "EASY_APPLY"
BRANCH_EXTERNAL_APPLY = "EXTERNAL_APPLY"
BRANCH_BLOCKED = "BLOCKED_HUMAN_REQUIRED"
BRANCH_UNAVAILABLE = "JOB_UNAVAILABLE"
# Distinct from BLOCKED — the user CAN proceed by logging in (or restoring a
# stored session). Surfacing this separately lets the orchestrator decide
# whether to retry with a candidate-authenticated session.
BRANCH_LOGIN_REQUIRED = "LOGIN_REQUIRED"

# Selectors observed on Indeed job-detail pages (both /viewjob?jk= and
# /cmp/.../jobs/...). Ordered easy-apply first because we prefer that branch.
_EASY_APPLY_SELECTORS: tuple[str, ...] = (
    "button#indeedApplyButton",
    "button[data-testid='indeedApplyButton']",
    "button[buttontype='IA']",
    "button:has-text('Apply with Indeed')",
    "button:has-text('Easily apply')",
    "a:has-text('Easily apply')",
)

_EXTERNAL_APPLY_SELECTORS: tuple[str, ...] = (
    "a:has-text('Apply on company site')",
    "a:has-text('Apply on employer site')",
    "button:has-text('Apply on company site')",
    "button:has-text('Apply on employer site')",
    "a:has-text('Apply now')",
    "button:has-text('Apply now')",
    "a[data-testid='applyButtonLinkContainer']",
)

# Phrases that mean the posting is closed. Substring match against page text.
_UNAVAILABLE_MARKERS: tuple[str, ...] = (
    "this job is no longer available",
    "job is no longer accepting applications",
    "this position has been filled",
    "job expired",
    "this job has expired",
    "the job you were trying to view",
)

# Substrings on the URL or page indicating we hit a bot wall / login gate /
# CAPTCHA before reaching the apply button. We never bypass these.
_BLOCKED_URL_MARKERS: tuple[str, ...] = (
    "/account/login",
    "secure.indeed.com",
    "/secure/",
    "challenge",
    "captcha",
)
_BLOCKED_TEXT_MARKERS: tuple[str, ...] = (
    "verify you are human",
    "verify that you're a human",
    "additional verification required",
    "unusual activity from your network",
    "checking your browser",
)


def _is_indeed_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith("indeed.com") or host.endswith("indeed.net")


def _is_smartapply_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "smartapply.indeed.com" or host.endswith(".smartapply.indeed.com")


class IndeedAdapter(BasePlatformAdapter):
    platform_name = "indeed"
    container_selector = None

    def __init__(self) -> None:
        self._inner: Optional[BasePlatformAdapter] = None
        self._branch: Optional[str] = None
        self._resolved_url: Optional[str] = None
        # Mirrored from inner adapter so executor's getattr() works
        # (executor.py:249, 335).
        self._iframe_mode: bool = False
        self._frame_locator = None
        self._frame = None

    # ──────────────────────────────────────────────────────────────────────
    # Branch classification
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    async def _detect_blocker(page: Page) -> Optional[str]:
        """Return a reason string if the page is gated, else None.

        Reason strings are prefixed so callers can tell BLOCKED from UNAVAILABLE
        without re-scanning text: "blocked: ..." vs "unavailable: ...".
        """
        url = (page.url or "").lower()
        for needle in _BLOCKED_URL_MARKERS:
            if needle in url:
                return f"blocked: URL marker {needle!r}"
        # Page <title> is the cheapest, most reliable bot-wall signal on
        # Indeed — they literally set <title>Blocked - Indeed.com</title>.
        try:
            title = (await page.title()).lower()
        except Exception:
            title = ""
        if "blocked" in title or "access denied" in title or "captcha" in title:
            return f"blocked: title={title!r}"
        try:
            body_text = (await page.locator("body").inner_text(timeout=3_000)).lower()
        except Exception:
            body_text = ""
        for needle in _BLOCKED_TEXT_MARKERS:
            if needle in body_text:
                return f"blocked: text marker {needle!r}"
        for needle in _UNAVAILABLE_MARKERS:
            if needle in body_text:
                return f"unavailable: text marker {needle!r}"
        return None

    @staticmethod
    async def _find_first_visible(page: Page, selectors: tuple[str, ...]):
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0:
                    continue
                if await loc.is_visible(timeout=1_500):
                    return sel, loc
            except Exception:
                continue
        return None, None

    # How long to poll for an apply button before giving up. Indeed renders
    # the apply CTA via client-side React 3-7s after domcontentloaded on slow
    # postings; checking once right after navigation misses it. Bump if you
    # see UNAVAILABLE on pages where the button is actually visible after a
    # few seconds in a real browser.
    _APPLY_BUTTON_POLL_TIMEOUT_MS = 12_000
    _APPLY_BUTTON_POLL_INTERVAL_MS = 750

    async def _classify_branch(self, page: Page) -> tuple[str, Optional[object]]:
        """Decide EASY_APPLY / EXTERNAL_APPLY / BLOCKED / UNAVAILABLE.

        Returns (branch, locator-or-None). The locator is the button to click
        for the apply-branches; None for terminal states.

        Polls for the apply button instead of one-shot checking — Indeed's
        React renderer mounts the CTA several seconds after page load on
        slower postings.
        """
        # Blockers are stable from first paint; check once.
        blocker = await self._detect_blocker(page)
        if blocker:
            logger.info(f"[Indeed] Blocker detected: {blocker}")
            if blocker.startswith("unavailable:"):
                return BRANCH_UNAVAILABLE, None
            return BRANCH_BLOCKED, None

        # Poll for an apply button. Easy Apply wins over External when both
        # mount in the same tick (rare, but well-defined precedence).
        deadline = asyncio.get_event_loop().time() + self._APPLY_BUTTON_POLL_TIMEOUT_MS / 1000
        attempt = 0
        last_branch_found = None
        while True:
            attempt += 1
            sel, loc = await self._find_first_visible(page, _EASY_APPLY_SELECTORS)
            if loc is not None:
                logger.info(
                    f"[Indeed] EASY_APPLY detected via {sel!r} (attempt {attempt})"
                )
                return BRANCH_EASY_APPLY, loc

            sel, loc = await self._find_first_visible(page, _EXTERNAL_APPLY_SELECTORS)
            if loc is not None:
                gate = await self._detect_login_gate(page)
                if gate:
                    logger.info(
                        f"[Indeed] EXTERNAL_APPLY login gate detected pre-click "
                        f"(matched {gate!r}) — LOGIN_REQUIRED (attempt {attempt})"
                    )
                    return BRANCH_LOGIN_REQUIRED, None
                logger.info(
                    f"[Indeed] EXTERNAL_APPLY detected via {sel!r} (attempt {attempt})"
                )
                return BRANCH_EXTERNAL_APPLY, loc

            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(self._APPLY_BUTTON_POLL_INTERVAL_MS / 1000)

        logger.warning(
            f"[Indeed] No apply button found after "
            f"{self._APPLY_BUTTON_POLL_TIMEOUT_MS/1000:.0f}s ({attempt} attempts)"
        )
        return BRANCH_UNAVAILABLE, None

    # ──────────────────────────────────────────────────────────────────────
    # External branch — follow click to destination ATS, delegate
    # ──────────────────────────────────────────────────────────────────────

    # Phrases Indeed prints on the job-detail page or in the post-click modal
    # to indicate the external-apply URL is gated behind authentication.
    # Observed verbatim on production listings 2026-06; lower-cased.
    _LOGIN_GATE_PHRASES: tuple[str, ...] = (
        "you must create an indeed account before continuing",
        "create an indeed account before continuing to the company",
        "sign in to continue to the company",
        "sign in to apply on the company",
        "continue with apple",
        "continue with google",
        "continue with email",
    )

    @classmethod
    async def _detect_login_gate(cls, page: Page) -> Optional[str]:
        """Return the matched phrase if Indeed's auth gate is on screen, else None.

        Two surfaces to check:
          1. Inline notice on the job-detail page (e.g. "You must create an
             Indeed account before continuing…") — appears BEFORE any modal.
          2. A role=dialog / aria-modal popup with Continue-with-Apple/Google
             federated-auth buttons.
        """
        try:
            sig = await page.evaluate(
                r"""
                () => {
                    const fullText = (document.body.innerText || '').toLowerCase();
                    const dialogs = document.querySelectorAll('[role=dialog], [aria-modal=true]');
                    let dialogText = '';
                    for (const d of dialogs) {
                        if (d.offsetWidth || d.offsetHeight) {
                            dialogText += ' ' + (d.innerText || '').toLowerCase();
                        }
                    }
                    const authIframes = Array.from(document.querySelectorAll('iframe'))
                        .map(f => f.src || '')
                        .filter(s => /accounts\.google|appleid|fedcm/i.test(s)).length;
                    return { fullText, dialogText, authIframes };
                }
                """
            )
        except Exception:
            return None

        haystack = (sig.get("fullText") or "") + " " + (sig.get("dialogText") or "")
        for phrase in cls._LOGIN_GATE_PHRASES:
            if phrase in haystack:
                return phrase
        if sig.get("authIframes", 0) > 0:
            return "federated-auth iframe present"
        return None

    async def _follow_external_apply(self, page: Page, btn) -> tuple[Optional[str], Optional[str]]:
        """Click the external-apply button; return (destination_url, status).

        Returns one of:
          (url, None)                  — happy path, destination ATS reached
          (None, BRANCH_LOGIN_REQUIRED) — Indeed login modal blocks the redirect
          (None, BRANCH_BLOCKED)       — CAPTCHA / unknown gate after click
          (None, None)                 — click had no observable effect
        """
        context = page.context

        # First try: popup tab. Most non-gated postings open the company URL
        # in a new tab. Wrap in expect_page so we don't miss fast popups.
        try:
            async with context.expect_page(timeout=_POPUP_WAIT_MS) as popup_info:
                await btn.click()
            popup = await popup_info.value
            try:
                await popup.wait_for_load_state("domcontentloaded", timeout=_REDIRECT_WAIT_MS)
            except Exception:
                pass
            url = popup.url
            logger.info(f"[Indeed] External apply opened popup → {url!r}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                await popup.close()
            except Exception as exc:
                logger.warning(f"[Indeed] Could not migrate popup→main: {exc}; using popup")
                return url, None
            return page.url, None
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            logger.debug(f"[Indeed] No popup ({exc}); checking for same-tab redirect or modal")

        # Same-tab redirect — give it up to _REDIRECT_WAIT_MS to leave indeed.com.
        try:
            await page.wait_for_url(
                lambda u: not _is_indeed_host(u),
                timeout=_REDIRECT_WAIT_MS,
            )
            url = page.url
            logger.info(f"[Indeed] External apply same-tab redirect → {url!r}")
            return url, None
        except Exception:
            pass

        # No redirect, no popup. The click did something — what? Check for
        # Indeed's federated-login modal. If it's up, that's LOGIN_REQUIRED
        # and we stop (SKILLS.md: never bypass login).
        if await self._detect_login_gate(page):
            logger.info(
                "[Indeed] External apply click triggered Indeed login modal — "
                "LOGIN_REQUIRED. Authenticate Sabih in a headed session and "
                "persist cookies via BrowserContextManager.save_session() so "
                "subsequent clicks land directly on the company site."
            )
            return None, BRANCH_LOGIN_REQUIRED

        # Modal not detected but no navigation either — check for a generic
        # blocker (CAPTCHA / challenge that appeared post-click).
        blocker = await self._detect_blocker(page)
        if blocker:
            logger.warning(f"[Indeed] Post-click blocker: {blocker}")
            return None, BRANCH_BLOCKED

        logger.warning("[Indeed] External apply click produced no observable navigation or modal")
        return None, None

    # ──────────────────────────────────────────────────────────────────────
    # BasePlatformAdapter
    # ──────────────────────────────────────────────────────────────────────

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        # Lazy import — avoids circular with adapters/__init__.py.
        from .registry import get_adapter

        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        except Exception as exc:
            logger.warning(f"[Indeed] goto {job_url!r} failed: {exc}")
            self._branch = BRANCH_BLOCKED
            self._inner = self._spawn_delegate("generic")
            return

        # Cookie banner dismissal — Indeed shows OneTrust on EU/UK locales.
        # Cheap best-effort, ignore failures.
        for sel in ("#onetrust-accept-btn-handler", "button:has-text('Accept all')"):
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible(timeout=1_000):
                    await btn.click(timeout=2_000)
                    await asyncio.sleep(0.3)
                    break
            except Exception:
                pass

        branch, btn = await self._classify_branch(page)
        self._branch = branch

        if branch == BRANCH_EASY_APPLY:
            # Click the Easy Apply button; SmartApply loads in the same tab.
            try:
                await btn.click(timeout=5_000)
                # Wait for the SmartApply host to appear.
                await page.wait_for_url(
                    lambda u: _is_smartapply_host(u) or "smartapply" in (u or "").lower(),
                    timeout=_REDIRECT_WAIT_MS,
                )
            except Exception as exc:
                logger.warning(f"[Indeed] Easy Apply click did not reach SmartApply: {exc}")
                # Fall through — the AgentLoop will see whatever loaded and
                # decide. Use generic adapter so its helpers are available.
                self._inner = self._spawn_delegate("generic")
                return
            # Use the generic adapter as the inner — SmartApply has no
            # boards.greenhouse.io-style iframe and no per-tenant quirks
            # beyond the smartapply hints (which the loop reads via
            # platform='smartapply').
            self._inner = self._spawn_delegate("generic")
            self._resolved_url = page.url
            logger.info(f"[Indeed] Easy Apply form ready at {self._resolved_url!r}")

        elif branch == BRANCH_EXTERNAL_APPLY:
            resolved, status = await self._follow_external_apply(page, btn)
            if status in (BRANCH_LOGIN_REQUIRED, BRANCH_BLOCKED):
                # Terminal — caller will see self._branch and abort cleanly.
                self._branch = status
                self._inner = self._spawn_delegate("generic")
                return
            if not resolved:
                logger.warning("[Indeed] External apply did not produce a destination URL")
                self._inner = self._spawn_delegate("generic")
                return
            # Hand off to the matching ATS adapter based on the destination
            # host. This is the same `get_adapter(url)` trick used elsewhere.
            self._inner = self._spawn_delegate(resolved)
            self._resolved_url = resolved
            logger.info(
                f"[Indeed] External apply → delegating to "
                f"{self._inner.platform_name!r} adapter at {resolved!r}"
            )
            # Some ATS adapters do extra setup (iframe detection, cookie
            # banners) in navigate_to_application — call it with the resolved
            # URL so they can re-initialise as if they owned navigation.
            try:
                await self._inner.navigate_to_application(page, resolved)
            except Exception as exc:
                logger.warning(
                    f"[Indeed] Inner adapter navigate_to_application raised: {exc}"
                )

        else:
            # BLOCKED or UNAVAILABLE — caller (AgentLoop) will see the
            # branch attribute and abort with the right structured reason.
            logger.info(f"[Indeed] Terminal branch={branch!r} — no inner adapter")
            self._inner = self._spawn_delegate("generic")

        # Mirror inner-adapter frame state up to self.
        self._iframe_mode = getattr(self._inner, "_iframe_mode", False)
        self._frame_locator = getattr(self._inner, "_frame_locator", None)
        self._frame = getattr(self._inner, "_frame", None)

    async def detect_application_type(self, page: Page) -> str:
        # For the AgentLoop's prompt: 'smartapply' on Easy Apply (so the
        # smartapply hints get injected), the inner adapter's name on
        # External, and 'indeed' otherwise.
        if self._branch == BRANCH_EASY_APPLY:
            return "smartapply"
        if self._inner and self._branch == BRANCH_EXTERNAL_APPLY:
            try:
                return await self._inner.detect_application_type(page)
            except NotImplementedError:
                return self._inner.platform_name
        return self.platform_name

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
        if self._branch in (BRANCH_BLOCKED, BRANCH_UNAVAILABLE, BRANCH_LOGIN_REQUIRED):
            logger.info(f"[Indeed] fill_application skipped — branch={self._branch!r}")
            return False
        if not self._inner:
            return False
        return await self._inner.fill_application(
            page, profile, resume_path, cover_letter_path,
            screening_answers, pre_detected_form=pre_detected_form,
            candidate_id=candidate_id,
        )

    async def submit(self, page: Page) -> bool:
        if self._branch in (BRANCH_BLOCKED, BRANCH_UNAVAILABLE, BRANCH_LOGIN_REQUIRED) or not self._inner:
            return False
        return await self._inner.submit(page)

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        if self._branch in (BRANCH_BLOCKED, BRANCH_UNAVAILABLE, BRANCH_LOGIN_REQUIRED) or not self._inner:
            return (False, self._branch)
        return await self._inner.verify_success(page)

    async def refresh_frame(self, page: Page) -> None:
        if self._inner and hasattr(self._inner, "refresh_frame"):
            await self._inner.refresh_frame(page)
            self._iframe_mode = getattr(self._inner, "_iframe_mode", False)
            self._frame_locator = getattr(self._inner, "_frame_locator", None)
            self._frame = getattr(self._inner, "_frame", None)
