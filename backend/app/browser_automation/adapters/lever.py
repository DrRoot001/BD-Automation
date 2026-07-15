"""Lever (jobs.lever.co) adapter.

Lever's hosted boards follow this URL pattern::

    https://jobs.lever.co/<company>/<job-uuid>          ← job description page
    https://jobs.lever.co/<company>/<job-uuid>/apply    ← application form page

The only Lever-specific thing is the ``/apply`` URL shortcut (skips an Apply
click). Everything else — filling, submitting, confirming, and any Lever quirk
(required LinkedIn URL, etc.) — is handled by the shared perception loop, with
Lever's selectors/quirks supplied to the reasoner as hints.
"""
from __future__ import annotations

from playwright.async_api import Page

from .autonomous_base import AutonomousAdapter


class LeverAdapter(AutonomousAdapter):
    platform_name = "lever"
    hints_key = "lever"
    container_selector = "#application-form"

    async def _resolve_target_url(self, page: Page, job_url: str) -> str:
        # Go straight to the form page; the loop still handles the listing case
        # (it will click Apply) if the shortcut doesn't land on a form.
        target = job_url.rstrip("/")
        return target if target.endswith("/apply") else target + "/apply"
