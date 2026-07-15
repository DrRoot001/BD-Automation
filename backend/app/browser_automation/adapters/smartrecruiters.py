"""SmartRecruiters (jobs.smartrecruiters.com) adapter.

URL patterns::

    https://jobs.smartrecruiters.com/<Company>/<postingId>-<slug>   (posting)
    https://jobs.smartrecruiters.com/oneclick-ui/company/...        (apply form)

Reaching the oneclick-ui form is SmartRecruiters-specific and kept here: the
posting's "I'm interested" CTA must be CLICKED (the SPA mints a fresh,
session-bound publication URL — a copied href 404s), and the form is built from
``<spl-*>`` web components in SHADOW DOM. The shared perception loop pierces open
shadow roots, so filling / confirm-email / consent / submit are all handled by
it; SmartRecruiters quirks are supplied as hints.
"""
from __future__ import annotations

import logging

from playwright.async_api import Page

from .autonomous_base import AutonomousAdapter
from ..agent import get_learned_fixes

logger = logging.getLogger(__name__)

_APPLY_SELECTORS = [
    "a[href*='/oneclick-ui/']",
    "a:has-text(\"I'm interested\")",
    "button:has-text(\"I'm interested\")",
    "a:has-text('Interested')",
]


class SmartRecruitersAdapter(AutonomousAdapter):
    platform_name = "smartrecruiters"
    hints_key = "smartrecruiters"
    container_selector = None  # oneclick-ui form is full page

    async def prepare(self, page: Page) -> None:
        self._resolved_url = page.url
        if "/oneclick-ui/" in (page.url or ""):
            await self._wait_for_form(page)
            return

        # Click "I'm interested" so the SPA navigates + hydrates the form.
        winning_sel = None
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
                    for p in page.context.pages:  # some tenants open a new tab
                        if "/oneclick-ui/" in (p.url or ""):
                            page = p
                            break
                if "/oneclick-ui/" in (page.url or ""):
                    winning_sel = sel
                    break
            except Exception as exc:
                logger.debug(f"[SmartRecruiters] apply CTA {sel!r} failed: {exc}")

        self._resolved_url = page.url
        if winning_sel and "/oneclick-ui/" in (page.url or ""):
            get_learned_fixes(self.platform_name).add("apply_button", winning_sel)
        if "/oneclick-ui/" in (page.url or ""):
            await self._wait_for_form(page)
        else:
            logger.warning(
                f"[SmartRecruiters] oneclick-ui not reached deterministically "
                f"(url={page.url!r}); the shared loop will find the CTA."
            )

    @staticmethod
    async def _wait_for_form(page: Page) -> None:
        try:
            await page.locator("#first-name-input").first.wait_for(state="attached", timeout=15_000)
            logger.info("[SmartRecruiters] oneclick-ui form hydrated")
        except Exception:
            logger.warning("[SmartRecruiters] form not detected after 15s — loop will re-perceive")
