"""LinkedIn Easy Apply adapter.

LinkedIn requires an authenticated session (seeded via BrowserContextManager's
Redis/storage-state restore). Two things are LinkedIn-specific and kept here:
detecting the auth wall (so we surface a clear LOGIN_REQUIRED and clear the stale
session) and opening the Easy Apply modal. Once the modal is open, the shared
perception loop walks its 1–5 steps (fill → Next → Submit) and confirms success;
LinkedIn's selectors are supplied as hints.
"""
from __future__ import annotations

import logging

from playwright.async_api import Page

from .autonomous_base import AutonomousAdapter
from ..agent import get_learned_fixes

logger = logging.getLogger(__name__)

_MODAL_SEL = ".jobs-easy-apply-modal, [aria-labelledby*='easy-apply']"

_EASY_APPLY_SELECTORS = [
    "button.jobs-apply-button:has-text('Easy Apply')",
    ".jobs-apply-button",
    "[data-job-id] .artdeco-button--primary",
    "button:has-text('Easy Apply')",
]

_AUTH_WALL_HINTS = (
    "sign in to view",
    "join now to view",
    "join now to continue",
    "sign in to linkedin",
)


class LinkedInEasyApplyAdapter(AutonomousAdapter):
    platform_name = "linkedin"
    hints_key = "linkedin"
    container_selector = ".jobs-easy-apply-modal"
    login_gated = True

    async def prepare(self, page: Page) -> None:
        # Auth-wall check — LinkedIn has no automated login (cookies are seeded).
        try:
            content = (await page.content()).lower()
        except Exception:
            content = ""
        if any(h in content[:6_000] for h in _AUTH_WALL_HINTS):
            try:
                from .session_utils import invalidate_session_file
                invalidate_session_file("linkedin")
            except Exception:
                pass
            raise RuntimeError(
                "LOGIN_REQUIRED: LinkedIn session expired or unauthenticated. "
                "Stale session file cleared — re-seed cookies via "
                "context_manager.save_session() for this candidate."
            )

        # Open the Easy Apply modal so the shared loop can drive it.
        learned = get_learned_fixes("linkedin").get("easy_apply_button")
        for sel in learned + [s for s in _EASY_APPLY_SELECTORS if s not in learned]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=6_000)
                    get_learned_fixes("linkedin").add("easy_apply_button", sel)
                    logger.info(f"[LinkedIn] Easy Apply opened via {sel!r}")
                    break
            except Exception as exc:
                logger.debug(f"[LinkedIn] Easy Apply selector {sel!r} failed: {exc}")
        else:
            raise RuntimeError(
                "BLOCKED: Easy Apply button not found. Job may not support Easy "
                "Apply, session may be unauthenticated, or LinkedIn DOM shifted."
            )

        try:
            await page.locator(_MODAL_SEL).first.wait_for(state="visible", timeout=8_000)
        except Exception:
            logger.warning("[LinkedIn] Easy Apply modal did not appear within 8s")
        await self.human_delay(0.5, 1.0)
