"""Workday (myworkdayjobs.com) adapter.

Workday-specific concerns kept here (auth + reaching the form):
  1. **Apply gate** — click "Apply" then "Apply Manually".
  2. **Account wall** — Workday requires an account; sign in with
     WORKDAY_USERNAME/PASSWORD (or the candidate's credentials) or surface a
     clear LOGIN_REQUIRED.

The multi-step questionnaire, custom widgets, submit and confirmation are all
handled by the shared perception loop (it re-observes each step, so it needs no
hard-coded step count); Workday's data-automation-id selectors are hints.
"""
from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Page

from .autonomous_base import AutonomousAdapter
from ..agent import get_learned_fixes

logger = logging.getLogger(__name__)

_APPLY_SELECTORS = [
    "[data-automation-id='applyToJobButton']",
    "[data-automation-id='adventureButton']",
    "[data-automation-id='applyButton']",
    "[data-automation-id='applyNow']",
    "[data-automation-id='applyManually']",
    "button[aria-label*='Apply' i]",
    "a[aria-label*='Apply' i]",
    "button:has-text('Apply Now')",
    "button:has-text('Apply Manually')",
    "button:has-text('Apply')",
    "a:has-text('Apply Now')",
    "a:has-text('Apply Manually')",
    "a:has-text('Apply')",
    "div[role='button']:has-text('Apply')",
    "[role='button']:has-text('Apply')",
]
_APPLY_MANUALLY_SELECTORS = [
    "[data-automation-id='applyManually']",
    "a:has-text('Apply Manually')",
    "button:has-text('Apply Manually')",
    "div[role='button']:has-text('Apply Manually')",
]
_SIGNIN_LINK_SELECTORS = [
    "[data-automation-id='signInLink']",
    "button:has-text('Sign In')",
    "a:has-text('Sign In')",
]


class WorkdayAdapter(AutonomousAdapter):
    platform_name = "workday"
    hints_key = "workday"
    container_selector = None
    login_gated = True

    async def prepare(self, page: Page) -> None:
        await self.human_delay(1.0, 2.0)
        # Wait for the React SPA to hydrate the Apply button.
        try:
            await page.wait_for_selector(
                "[data-automation-id='applyToJobButton'], "
                "[data-automation-id='adventureButton'], "
                "[data-automation-id='applyButton'], "
                "[data-automation-id='applyNow'], "
                "[data-automation-id='applyManually']",
                state="visible", timeout=12_000,
            )
        except Exception:
            logger.info("[Workday] Apply not visible in 12s — trying text selectors")
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass

        if not await self._click_one_of(page, "apply_button", _APPLY_SELECTORS):
            # Soft-fail: let the shared loop's vision find the Apply button.
            logger.warning(f"[Workday] Apply not found via selectors — loop recovers. url={page.url}")
            return
        await self.human_delay(0.6, 1.2)

        try:
            await page.locator(", ".join(_APPLY_MANUALLY_SELECTORS)).first.wait_for(
                state="visible", timeout=4_000
            )
            await self._click_one_of(page, "apply_manually", _APPLY_MANUALLY_SELECTORS)
        except Exception:
            logger.debug("[Workday] no Apply Manually chooser appeared")
        await self.human_delay(0.6, 1.2)

        await self._sign_in_if_required(page)

        try:
            await page.locator("input, textarea, select").first.wait_for(state="attached", timeout=15_000)
        except Exception:
            logger.warning("[Workday] form inputs did not attach within 15s; proceeding")

    # ── helpers (kept: auth + Workday-specific clicking) ──────────────────────
    async def _click_one_of(self, page: Page, channel: str, candidates: list) -> bool:
        learned = get_learned_fixes("workday").get(channel)
        for sel in learned + [s for s in candidates if s not in learned]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=5_000)
                    get_learned_fixes("workday").add(channel, sel)
                    logger.info(f"[Workday] {channel} via {sel!r}")
                    return True
            except Exception as exc:
                logger.debug(f"[Workday] {channel} selector {sel!r} failed: {exc}")
        return False

    async def _sign_in_if_required(self, page: Page) -> None:
        try:
            visible_signin = await page.locator(", ".join(_SIGNIN_LINK_SELECTORS)).first.is_visible(timeout=3_000)
        except Exception:
            visible_signin = False
        if not visible_signin:
            return
        username = self._login_credential("login_email", "WORKDAY_USERNAME")
        password = self._login_credential("password", "WORKDAY_PASSWORD")
        if not username or not password:
            raise RuntimeError(
                "LOGIN_REQUIRED: Workday requires an account; set WORKDAY_USERNAME/"
                "WORKDAY_PASSWORD in .env or add gmail+password to the candidate profile"
            )
        logger.info("[Workday] signing in with stored credentials")
        await self._click_one_of(page, "signin_link", _SIGNIN_LINK_SELECTORS)
        await self.human_delay(0.5, 1.0)
        try:
            await page.locator("[data-automation-id='email']").fill(username, timeout=5_000)
            await page.locator("[data-automation-id='password']").fill(password, timeout=5_000)
            await page.locator("[data-automation-id='click_filter']").click(timeout=5_000)
            await page.wait_for_load_state("domcontentloaded", timeout=12_000)
        except Exception as exc:
            raise RuntimeError(f"BLOCKED: Workday sign-in failed: {exc}")
        await asyncio.sleep(1.5)
