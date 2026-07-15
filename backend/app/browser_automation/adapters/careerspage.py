"""Careers Page (Manatal ATS) adapter — careers-page.com hosted application forms.

``careers-page.com`` is a hosted Applicant Tracking System powered by Manatal,
where employers publish a SINGLE-PAGE application form (Personal Information,
Professional Information, Salary + Notice Period, mandatory Resume upload, a
required Privacy/Terms checkbox, then Submit). It is reached either directly
(a scraped careers-page.com job) or via a job-board external-apply redirect
(e.g. Remote100K -> careers-page.com).

Platform detection is by HOST ONLY (``careers-page.com``) — never the job id,
company slug, or query params (per spec). The form itself is filled by the
shared perception loop / AgentLoop using the detailed ``careerspage`` hints in
``hints.py`` (verified against the real Manatal DOM). The two platform-specific
concerns kept here are:

  * land on the ``/apply`` form URL (``careerspage_apply_url``); and
  * deterministically fix the salary CURRENCY / FREQUENCY selects
    (``apply_manatal_salary_format``): Manatal defaults them to their FIRST
    option ('Barbados dollar' / 'Hourly'), and because they always carry a value
    the form's required-field gate never flags them — so the agent would
    otherwise submit a nonsensical salary. These are format fields, not
    candidate facts, so we set them deterministically to US Dollar / Yearly.
Both helpers are module-level so the RemoteRocketship/Remote100K passthrough can
reuse them when a listing redirects into careers-page.com.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlparse

from playwright.async_api import Page

from .autonomous_base import AutonomousAdapter

logger = logging.getLogger(__name__)

_USD_OPTION_TEXTS = ("us dollar", "u.s. dollar", "united states dollar", "usd")


def careerspage_apply_url(url: str) -> str:
    """Normalize a Manatal job URL to its ``/apply`` form path. Only ever appends
    ``/apply`` to a ``/job/<id>`` path so we never fabricate a bad URL."""
    try:
        path = (urlparse(url).path or "").rstrip("/")
    except Exception:
        return url
    if not path or path.endswith("/apply"):
        return url
    if "/job/" in path:
        return url.split("?")[0].rstrip("/") + "/apply"
    return url


async def apply_manatal_salary_format(page: Any, attempts: int = 8, delay: float = 0.7) -> bool:
    """Set the Manatal salary CURRENCY select -> 'US Dollar' and the FREQUENCY
    select -> 'Yearly' (they default to 'Barbados dollar' / 'Hourly').

    The CURRENCY select's ~120 options are populated by JS a beat AFTER the form
    renders, so we POLL: re-scan the selects each attempt until BOTH the currency
    ('US Dollar' present) and frequency selects have been set, or we time out.
    A no-op when the form has no such selects. Never raises. Returns True if any
    select was changed."""
    try:
        await page.wait_for_selector("select", timeout=6_000)
    except Exception:
        return False
    changed = False
    freq_done = False
    cur_done = False
    for _ in range(max(1, attempts)):
        try:
            selects = await page.locator("select").all()
        except Exception:
            break
        for sel in selects:
            try:
                opts = await sel.evaluate("s => Array.from(s.options).map(o => o.text.trim())")
            except Exception:
                continue
            opts_l = [o.lower() for o in opts]
            # Frequency select: identified by having both Hourly and Yearly.
            if not freq_done and "yearly" in opts_l and "hourly" in opts_l:
                if await _set_if_needed(sel, next(o for o in opts if o.lower() == "yearly")):
                    changed = True
                freq_done = True
                continue
            # Currency select: identified by a 'US Dollar' option (present only
            # once the currency list has finished populating).
            if not cur_done:
                usd = next((o for o in opts if o.strip().lower() in _USD_OPTION_TEXTS), None)
                if usd:
                    if await _set_if_needed(sel, usd):
                        changed = True
                    cur_done = True
        if freq_done and cur_done:
            break
        await asyncio.sleep(delay)
    if changed:
        logger.info("[CareersPage] salary format set deterministically: currency=US Dollar, frequency=Yearly")
    elif not (freq_done and cur_done):
        logger.debug(f"[CareersPage] salary selects not fully ready (freq={freq_done}, currency={cur_done})")
    return changed


async def _set_if_needed(sel: Any, label: str) -> bool:
    """select_option(label) unless already selected. Returns True if it changed."""
    try:
        cur = (await sel.evaluate("s => (s.options[s.selectedIndex]||{}).text || ''") or "").strip()
    except Exception:
        cur = ""
    if cur.lower() == label.lower():
        return False
    try:
        await sel.select_option(label=label)
        return True
    except Exception:
        return False


class CareersPageAdapter(AutonomousAdapter):
    platform_name = "careerspage"
    hints_key = "careerspage"
    container_selector = "form"

    async def _resolve_target_url(self, page: Page, job_url: str) -> Optional[str]:
        """The application form lives at ``.../job/<id>/apply``. Point straight at
        it when the scraped URL is the job description; the loop still handles a
        listing -> Apply click if the shortcut doesn't land on the form."""
        return careerspage_apply_url(job_url)

    async def prepare(self, page: Page) -> None:
        """Deterministically fix the salary currency/frequency selects before the
        loop runs (the reasoner otherwise leaves Manatal's wrong defaults)."""
        await apply_manatal_salary_format(page)
