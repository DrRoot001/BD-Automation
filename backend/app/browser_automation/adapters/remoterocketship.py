"""Remote Rocketship (+ RemoteOK / Adzuna / hiring.cafe / Remote100k) adapter.

These are job-aggregator wrappers, not ATSes: each listing's Apply link points
at an underlying ATS form (Greenhouse, Lever, Ashby, Workable, …). The listing
page itself has no form.

Migration note: the passthrough no longer instantiates an inner ATS *adapter* to
drive the form — the shared perception + reasoning loop drives ANY ATS. The
adapter's only job is to RESOLVE the underlying ATS URL (scrape the Apply anchor
in the real browser, since the CDN 403s plain httpx) and hand it to the shared
loop. We still detect the inner ATS from the URL so the reasoner gets that ATS's
hints instead of the aggregator's.
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import Page

from .autonomous_base import AutonomousAdapter

logger = logging.getLogger(__name__)

# ATS host → (required-path-substring, hints/registry key).
_ATS_HOSTS: tuple[tuple[str, str, str], ...] = (
    ("job-boards.greenhouse.io", "/jobs/", "greenhouse"),
    ("boards.greenhouse.io",      "/jobs/", "greenhouse"),
    ("greenhouse.io",             "/jobs/", "greenhouse"),
    ("jobs.lever.co",             "",       "lever"),
    ("lever.co",                  "",       "lever"),
    ("jobs.ashbyhq.com",          "",       "ashby"),
    ("ashbyhq.com",               "",       "ashby"),
    ("myworkdayjobs.com",         "",       "workday"),
    ("workday.com",               "",       "workday"),
    ("apply.workable.com",        "/j/",    "generic"),
    ("workable.com",              "/j/",    "generic"),
    ("ats.rippling.com",          "",       "generic"),
    ("jobs.smartrecruiters.com",  "",       "smartrecruiters"),
    ("jobs.jobvite.com",          "/job/",  "jobvite"),
    ("jobvite.com",               "/job/",  "jobvite"),
    ("pinpointhq.com",            "",       "generic"),
    # Careers Page — the hosted employer ATS powered by Manatal. Detect by host
    # only (spec: never rely on job id/slug/query). Enables aggregator handoff
    # AND the AgentLoop's mid-run reclassify to swap in the careerspage hints.
    ("careers-page.com",          "",       "careerspage"),
    # CareerPlug — Rails ATS. Job path is /jobs/<id> (redirects to /apps/new).
    ("careerplug.com",            "/jobs/", "careerplug"),
    # TeamTailor — hosted ATS. Catches its own *.teamtailor.com sites; the far
    # more common WHITE-LABEL career domains (careers.<company>.com) carry no
    # URL token and are detected by DOM signature (AgentLoop._detect_ats_from_dom).
    ("teamtailor.com",            "",       "teamtailor"),
    ("linkedin.com",              "/jobs/view/", "linkedin"),
)

_NON_APPLICATION_PATH_PATTERNS: tuple[str, ...] = (
    "/company/", "/in/", "/school/", "/about", "/login", "/signup",
    "/sign-in", "/contact", "/privacy", "/terms",
)


def _detect_ats_from_url(url: str) -> Optional[str]:
    """Map a URL to an ATS hints key, or None if it isn't a job application."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if not host:
        return None
    if "jobs.smartrecruiters.com" in host and "/oneclick-ui/" in path:
        return "smartrecruiters"
    for bad in _NON_APPLICATION_PATH_PATTERNS:
        if bad in path:
            return None
    for needle, required_path, key in _ATS_HOSTS:
        if needle in host:
            if required_path and required_path not in path:
                continue
            return key
    return None


class RemoteRocketshipAdapter(AutonomousAdapter):
    platform_name = "remoterocketship"
    hints_key = "remoterocketship"

    @staticmethod
    async def _scrape_apply_url(page: Page, rr_url: str) -> Optional[str]:
        """Load the RR listing in the real (stealth) browser and extract the ATS
        application URL from its anchors."""
        try:
            await page.goto(rr_url, wait_until="domcontentloaded", timeout=20_000)
        except Exception as exc:
            logger.warning(f"[RR] page.goto {rr_url!r} failed: {exc}")
            return None
        try:
            hrefs = await page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".map(a => a.href).filter(h => h.startsWith('http'))"
            )
        except Exception as exc:
            logger.warning(f"[RR] DOM scrape failed: {exc}")
            hrefs = []
        for href in hrefs or []:
            if _detect_ats_from_url(href):
                logger.info(f"[RR] Resolved {rr_url!r} → {href!r}")
                return href
        logger.warning(f"[RR] No ATS URL found in {rr_url!r} (scanned {len(hrefs or [])} anchors)")
        return None

    async def _resolve_target_url(self, page: Page, job_url: str) -> Optional[str]:
        # Already a resolved inner ATS URL (pipeline sometimes stores it).
        hostname = (urlparse(job_url).hostname or "").lower()
        direct_key = _detect_ats_from_url(job_url)
        if direct_key and "remoterocketship" not in hostname and "remote100k" not in hostname:
            self.hints_key = direct_key
            logger.info(f"[RR] URL is already a resolved {direct_key!r} ATS")
            return self._normalize_inner_url(direct_key, job_url)

        resolved = await self._scrape_apply_url(page, job_url)
        if resolved:
            self.hints_key = _detect_ats_from_url(resolved) or "generic"
            resolved = self._normalize_inner_url(self.hints_key, resolved)
            logger.info(f"[RR] Using inner ATS hints={self.hints_key!r} for {resolved!r}")
            return resolved

        # No recognizable ATS link — stay on the current page and let the loop
        # click Apply (it re-observes; returning None skips the base goto).
        logger.info(f"[RR] No ATS link on {job_url!r}; letting the loop drive the current page")
        return None

    @staticmethod
    def _normalize_inner_url(ats_key: Optional[str], url: str) -> str:
        """Per-inner-ATS URL normalization. Currently: point a Manatal listing
        straight at its ``/apply`` form so the salary-format fixup + the loop see
        the form immediately (no separate Apply click)."""
        if ats_key == "careerspage":
            from .careerspage import careerspage_apply_url
            return careerspage_apply_url(url)
        return url

    async def prepare(self, page: Page) -> None:
        """When a listing redirected into careers-page.com (Manatal), fix its
        salary currency/frequency selects deterministically — same as the
        dedicated CareersPageAdapter — since the passthrough drives the shared
        loop directly on that form."""
        try:
            host = (urlparse(page.url or "").hostname or "").lower()
        except Exception:
            host = ""
        if "careers-page.com" in host:
            try:
                from .careerspage import apply_manatal_salary_format
                await apply_manatal_salary_format(page)
            except Exception as exc:
                logger.debug(f"[RR] manatal salary format skipped: {exc}")
