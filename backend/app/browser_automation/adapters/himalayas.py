"""Himalayas (himalayas.app) adapter — native Quick Apply OR ATS passthrough.

Two listing flavours:
  a. **Native Quick Apply** — a Himalayas-hosted form; the shared loop fills it.
  b. **External ATS** — the Apply CTA links to Greenhouse / Lever / Ashby / etc.;
     we resolve that URL and navigate to it, then the shared loop drives it.

Only the *navigation* (which branch, and reaching the form) is Himalayas-specific
and kept here. All decision logic (fill / submit / verify) comes from the shared
:class:`AutonomousAdapter` loop.
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import Page

from .autonomous_base import AutonomousAdapter
from .remoterocketship import _detect_ats_from_url

logger = logging.getLogger(__name__)

_NAV_TIMEOUT_MS = 30_000
_FIELD_TIMEOUT_MS = 8_000

_QUICK_APPLY_SELECTORS = (
    "button:has-text('Quick Apply')",
    "button:has-text('Quick apply')",
    "a:has-text('Quick apply')",
    "a:has-text('Quick Apply')",
)


class HimalayasAdapter(AutonomousAdapter):
    platform_name = "himalayas"
    hints_key = "himalayas"
    container_selector = None

    def __init__(self, agent=None) -> None:
        super().__init__(agent=agent)
        self._native_form: bool = False

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        self._job_url = job_url
        # 0. Already a resolved ATS URL → go straight there.
        direct_key = _detect_ats_from_url(job_url)
        hostname = (urlparse(job_url).hostname or "").lower()
        if direct_key and "himalayas" not in hostname:
            self.hints_key = direct_key
            self._resolved_url = job_url
            await self._goto(page, job_url)
            await self._resolve_frame(page)
            return

        await self._goto(page, job_url)

        # 1. Native Quick Apply — click and let the shared loop drive the form.
        for sel in _QUICK_APPLY_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=_FIELD_TIMEOUT_MS)
                self._native_form = True
                logger.info(f"[Himalayas] Quick Apply via {sel!r} — native form path")
                await self.human_delay(1.0, 2.0)
                return
            except Exception as exc:
                logger.debug(f"[Himalayas] Quick Apply {sel!r} failed: {exc}")

        # 2. External branch — resolve an ATS link and navigate to it.
        try:
            hrefs = await page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".map(a => a.href).filter(h => h.startsWith('http'))"
            )
        except Exception:
            hrefs = []
        resolved = next((h for h in (hrefs or []) if _detect_ats_from_url(h)), None)
        if resolved:
            self.hints_key = _detect_ats_from_url(resolved) or "generic"
            self._resolved_url = resolved
            logger.info(f"[Himalayas] Resolved external ATS ({self.hints_key}) → {resolved!r}")
            await self._goto(page, resolved)
            await self._resolve_frame(page)
            return

        # 3. Nothing matched — let the shared loop salvage the current page.
        logger.warning(f"[Himalayas] No Quick Apply / ATS link on {job_url!r}; loop drives current page")
