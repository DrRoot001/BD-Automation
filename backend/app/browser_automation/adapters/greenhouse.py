"""Greenhouse adapter.

Greenhouse uses two hosting modes:
  1. boards.greenhouse.io / job-boards.greenhouse.io — form rendered in the page.
  2. Embedded widget on a company site — form lives inside ``#grnhse_iframe``.

The only Greenhouse-specific concerns kept here are:
  * rewriting a mirrored careers URL (``?gh_jid=…``) to the canonical
    job-boards URL (with a HEAD probe so we don't chase a redirect loop), and
  * scoping the loop to ``#grnhse_iframe`` when embedded.

Everything else — Apply click, react-select dropdowns, the EEO/demographic
block, file uploads, submit, post-submit verification — is handled by the shared
perception + reasoning loop, with Greenhouse's quirks supplied as hints.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page

from .autonomous_base import AutonomousAdapter

logger = logging.getLogger(__name__)

# Apply CTAs on a company-embedded Greenhouse page. Clicking one INJECTS the
# #grnhse_iframe — so we must reveal it before the base scopes the loop to it.
_APPLY_SELECTORS = (
    "a#apply_button",
    "#nav_apply",
    "a[href*='#app']:has-text('Apply')",
    "a:has-text('Apply for this Job')",
    "button:has-text('Apply for this Job')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply Now')",
    "[data-qa='btn-apply']",
)


class GreenhouseAdapter(AutonomousAdapter):
    platform_name = "greenhouse"
    hints_key = "greenhouse"
    iframe_selector = "#grnhse_iframe"

    async def _resolve_target_url(self, page: Page, job_url: str) -> str:
        return await self._smart_rewrite_to_canonical(job_url)

    async def prepare(self, page: Page) -> None:
        # Reveal / enter the Greenhouse form so the base can scope the loop to it.
        # Two layouts:
        #   • boards.greenhouse.io — the Apply CTA is on the page (and may inject
        #     #grnhse_iframe or reveal the form in-page).
        #   • company-embedded — the LISTING (and its Apply link) live INSIDE
        #     #grnhse_iframe; clicking it makes the parent swap the iframe to the
        #     form. A page-level locator can't reach that link, so we also look
        #     inside the frame.
        # 1. Page-level Apply.
        for sel in _APPLY_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=6_000)
                    await self._settle_after_apply(page)
                    return
            except Exception as exc:
                logger.debug(f"[GH] page apply CTA {sel!r} failed: {exc}")

        # 2. In-iframe Apply (embedded listing inside #grnhse_iframe).
        try:
            if await page.locator(self.iframe_selector).count() > 0:
                fl = page.frame_locator(self.iframe_selector)
                for sel in ("a#apply_button", "a#apply_btn", "#apply_btn",
                            "a:has-text('Apply Now')", "a:has-text('Apply')",
                            "button:has-text('Apply')"):
                    try:
                        btn = fl.locator(sel).first
                        if await btn.count() > 0:
                            await btn.click(timeout=6_000)
                            await self._settle_after_apply(page)
                            return
                    except Exception:
                        continue
        except Exception as exc:
            logger.debug(f"[GH] in-iframe apply failed: {exc}")

    async def _settle_after_apply(self, page: Page) -> None:
        """Wait for the iframe (re)injection + destroy/recreate swap to settle."""
        try:
            await page.locator(self.iframe_selector).first.wait_for(state="attached", timeout=5_000)
        except Exception:
            pass
        # The embedded fixture/site re-injects the iframe ~500ms later; give the
        # final swap time to land before _resolve_frame waits on its content.
        await asyncio.sleep(0.9)

    # ── canonical-URL rewrite (kept: genuinely Greenhouse-specific) ───────────
    async def _smart_rewrite_to_canonical(self, job_url: str) -> str:
        """Rewrite a mirrored careers URL to job-boards.greenhouse.io, HEAD-probing
        the candidate so we fall back to the original on a redirect-loop / 404."""
        canonical = self._rewrite_to_canonical(job_url)
        if canonical == job_url:
            return job_url
        try:
            import httpx
            original_host = (urlparse(job_url).hostname or "").lower()
            async with httpx.AsyncClient(follow_redirects=False, timeout=6.0) as client:
                resp = await client.head(canonical)
            if 300 <= resp.status_code < 400:
                loc_host = (urlparse(resp.headers.get("location", "")).hostname or "").lower()
                if loc_host == original_host:
                    return job_url
            if resp.status_code in (404, 410):
                return job_url
        except Exception as exc:
            logger.warning(f"[GH] HEAD probe of {canonical!r} failed: {exc} — using canonical")
        return canonical

    @staticmethod
    def _rewrite_to_canonical(job_url: str) -> str:
        try:
            parsed = urlparse(job_url)
            host = (parsed.hostname or "").lower()
            if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
                return job_url
            gh_jid_list = parse_qs(parsed.query or "").get("gh_jid") or []
            if not gh_jid_list:
                return job_url
            gh_jid = gh_jid_list[0].strip()
            if not gh_jid.isdigit():
                return job_url
            host_parts = host.split(".")
            if host_parts and host_parts[0] in ("jobs", "careers", "www"):
                host_parts = host_parts[1:]
            slug = host_parts[0] if host_parts else ""
            if not slug:
                return job_url
            canonical = f"https://job-boards.greenhouse.io/{slug}/jobs/{gh_jid}"
            logger.info(f"[GH] Rewrote {job_url!r} -> {canonical!r}")
            return canonical
        except Exception as exc:
            logger.warning(f"[GH] Canonical rewrite failed for {job_url!r}: {exc}")
            return job_url
