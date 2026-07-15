"""Jobvite (jobs.jobvite.com) adapter.

URL patterns::

    https://jobs.jobvite.com/<company>/job/<jobId>          (posting)
    https://jobs.jobvite.com/<company>/job/<jobId>/apply    (application form)

Only the navigation (rewrite ``/job/<id>`` → ``/job/<id>/apply`` on Jobvite
hosts, else click the Apply anchor) is Jobvite-specific and kept here. Field
tokens are per-tenant random, the embedded reCAPTCHA, the multi-step Next flow,
and the duplicate-application banner are all handled by the shared loop (labels
+ perception + condition detection). Jobvite quirks are supplied as hints.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse, urlunparse

from playwright.async_api import Page

from .autonomous_base import AutonomousAdapter
from ..agent import get_learned_fixes

logger = logging.getLogger(__name__)

_APPLY_SELECTORS = [
    "a[href*='/apply']",
    "a:has-text('Apply')",
    "button:has-text('Apply')",
]


class JobviteAdapter(AutonomousAdapter):
    platform_name = "jobvite"
    hints_key = "jobvite"

    @staticmethod
    def _build_apply_url(job_url: str) -> str:
        parsed = urlparse(job_url)
        host = (parsed.hostname or "").lower()
        if "jobvite.com" not in host:
            return job_url
        path = parsed.path.rstrip("/")
        if path.endswith("/apply") or "/job/" not in path:
            return job_url
        return urlunparse(parsed._replace(path=path + "/apply"))

    async def _resolve_target_url(self, page: Page, job_url: str) -> str:
        return self._build_apply_url(job_url)

    async def prepare(self, page: Page) -> None:
        # If the /apply rewrite didn't land on the form, click the Apply anchor.
        if await self._form_present(page):
            return
        learned = get_learned_fixes(self.platform_name).get("apply_button")
        for sel in learned + [s for s in _APPLY_SELECTORS if s not in learned]:
            try:
                el = page.locator(sel).first
                if await el.count() == 0 or not await el.is_visible():
                    continue
                await el.scroll_into_view_if_needed()
                await el.click(timeout=5_000)
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                get_learned_fixes(self.platform_name).add("apply_button", sel)
                logger.info(f"[Jobvite] Apply clicked via {sel!r}")
                break
            except Exception as exc:
                logger.debug(f"[Jobvite] apply CTA {sel!r} failed: {exc}")

    @staticmethod
    async def _form_present(page: Page) -> bool:
        try:
            return await page.locator("input[id^='jv-field-']").count() > 0
        except Exception:
            return False
