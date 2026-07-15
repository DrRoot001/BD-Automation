"""iCIMS adapter.

The only iCIMS-specific concern kept here is loading the job page with bot-wall
detection + cookie-banner dismissal. The multi-step flow (Apply → email consent
→ profile + login creation → screening questions → submit) is driven entirely by
the shared perception loop; iCIMS selectors/quirks are supplied as hints. iCIMS
has no same-page form iframe, so the loop runs against the top-level page.
"""
from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Page

from .autonomous_base import AutonomousAdapter

logger = logging.getLogger(__name__)

_NAV_TIMEOUT_MS = 25_000
_FIELD_TIMEOUT_MS = 6_000

_BLOCK_PATTERNS = (
    "request could not be satisfied",
    "request blocked",
    "access denied",
    "too many requests",
)


class ICIMSAdapter(AutonomousAdapter):
    platform_name = "icims"
    hints_key = "icims"
    container_selector = ".iCIMS_MainWrapper"
    login_gated = True

    async def prepare(self, page: Page) -> None:
        # Wait for the iCIMS wrapper or the application form to exist.
        try:
            await page.wait_for_selector(
                "h1.iCIMS_Header, .iCIMS_MainWrapper, .iCIMS_CandidatePage, "
                "#iCIMS_ApplyOnlineLink, input.iCIMS_PrimaryButton",
                timeout=_FIELD_TIMEOUT_MS,
            )
        except Exception:
            logger.warning("[iCIMS] No iCIMS wrapper detected within timeout")

        # Bot-wall check.
        try:
            title = (await page.title()).lower()
        except Exception:
            title = ""
        snippet = ""
        try:
            snippet = (await page.content())[:1200].lower()
        except Exception:
            pass
        if any(p in title or p in snippet for p in _BLOCK_PATTERNS):
            logger.error(f"[iCIMS] Bot wall detected at {page.url!r}")
            raise RuntimeError(f"BLOCKED: iCIMS bot wall at {page.url}")

        # Dismiss cookie banners (OneTrust / iCIMS native consent).
        for sel in (
            "button#onetrust-accept-btn-handler",
            "button#onetrust-reject-all-handler",
            "button.osano-cm-accept",
            "button:has-text('Accept All')",
            "button:has-text('Accept')",
        ):
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=2_000)
                    logger.info(f"[iCIMS] Dismissed cookie banner via {sel!r}")
                    await asyncio.sleep(0.4)
                    break
            except Exception:
                continue
        await self.human_delay(0.5, 1.5)
