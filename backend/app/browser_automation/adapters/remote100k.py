"""Remote100K external-apply source — a dedicated aggregator passthrough.

Remote100K (``remote100k.com``) is a JOB SOURCE, not an ATS: each listing's
"Apply for this Job" control redirects to the employer's real ATS (Ashby,
Greenhouse, Lever, Careers Page/Manatal, …). Per the spec, Remote100K is treated
as a *source* that (1) opens the listing, (2) resolves the external ATS, and
(3) hands control to the shared perception loop with THAT ATS's hints. It never
owns an application form itself; success is decided by the downstream ATS.

It reuses the RemoteRocketship passthrough machinery (anchor scrape + URL->ATS
detection, both battle-tested). Remote100K's Apply is usually an ``<a href>`` to
the ATS (handled by the inherited anchor scrape), but is sometimes a JS button
that navigates; when the anchor scrape finds nothing we fall back to CLICKING
"Apply for this Job" and following the same-tab redirect, then detect the ATS
from the landed URL.
"""
from __future__ import annotations

import logging
from typing import Optional

from playwright.async_api import Page

from .remoterocketship import RemoteRocketshipAdapter, _detect_ats_from_url

logger = logging.getLogger(__name__)

_APPLY_SELECTORS: tuple[str, ...] = (
    "a:has-text('Apply for this Job')",
    "button:has-text('Apply for this Job')",
    "a:has-text('Apply for This Job')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply Now')",
    "a:has-text('Apply')",
    "button:has-text('Apply')",
)


class Remote100KAdapter(RemoteRocketshipAdapter):
    platform_name = "remote100k"
    hints_key = "remote100k"

    async def _resolve_target_url(self, page: Page, job_url: str) -> Optional[str]:
        # 1. Inherited resolution: direct-ATS URL passthrough, or anchor scrape
        #    of the listing (also does page.goto to load it). Returns a URL to
        #    navigate to, or None when no scrapable ATS anchor was found.
        resolved = await super()._resolve_target_url(page, job_url)
        if resolved is not None:
            return resolved

        # 2. Fallback: the Apply control is a JS button (no href). The listing is
        #    already loaded (super() did the goto); click Apply and follow the
        #    same-tab external redirect, then detect the ATS from the landed URL.
        followed = await self._click_apply_and_follow(page)
        if followed:
            self.hints_key = _detect_ats_from_url(followed) or "generic"
            # We navigated by clicking — record the resolved URL and return None
            # so the base does NOT issue a second goto (which would reload the
            # listing and lose the redirect).
            self._resolved_url = followed
            logger.info(
                f"[Remote100K] followed Apply redirect -> {followed!r} "
                f"(inner ATS hints={self.hints_key!r})"
            )
        else:
            logger.info(
                "[Remote100K] No external ATS resolved; letting the shared loop "
                "drive the current page (it re-observes and can click Apply)."
            )
        return None

    async def _click_apply_and_follow(self, page: Page) -> Optional[str]:
        """Click 'Apply for this Job' and follow a SAME-TAB redirect off
        remote100k.com to a recognizable ATS. Returns the landed ATS URL or None.
        New-tab redirects are left to the loop's popup adoption."""
        # Already on an ATS (e.g. a prior nav landed us there)?
        try:
            if _detect_ats_from_url(page.url or ""):
                return page.url
        except Exception:
            pass
        for sel in _APPLY_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                await btn.click(timeout=6_000)
                try:
                    await page.wait_for_url(
                        lambda u: bool(u) and "remote100k" not in u, timeout=20_000
                    )
                except Exception:
                    pass
                landed = page.url or ""
                if _detect_ats_from_url(landed):
                    return landed
            except Exception as exc:
                logger.debug(f"[Remote100K] apply click {sel!r} failed: {exc}")
                continue
        return None
