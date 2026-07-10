"""Remote Rocketship adapter.

remoterocketship.com is a job-aggregator wrapper, not an ATS. Each listing has
an "Apply" button whose href points at the underlying ATS form (Greenhouse,
Lever, Ashby, Workable, etc.). The listing page itself has NO form to fill.

Strategy (per operator decision): pre-resolve the underlying ATS URL by
loading RR in the existing Playwright page, scraping the Apply anchor from
the rendered DOM, then `goto` the resolved URL directly — no click, no
new-tab dance. Once the resolved URL loads, ALL adapter behaviour delegates
to the matching ATS adapter (Greenhouse / Lever / Ashby / …).

We use Playwright instead of plain httpx because RR is fronted by a bot wall
that 403s headless HTTP requests. Playwright is already running a real
browser context with stealth applied, so the page loads normally.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import Page

from .base import BasePlatformAdapter

logger = logging.getLogger(__name__)

# ATS host → (required-path-substring, registry-key). Path substring filters
# out company/social pages on hosts that ALSO serve non-application content
# (e.g. linkedin.com/company/X is a social page, NOT an application — only
# linkedin.com/jobs/view/<id> is). Empty string means "host alone is enough".
# Order matters only for prefix-substring fallback (job-boards before boards).
_ATS_HOSTS: tuple[tuple[str, str, str], ...] = (
    ("job-boards.greenhouse.io", "/jobs/",        "greenhouse"),
    ("boards.greenhouse.io",      "/jobs/",        "greenhouse"),
    ("greenhouse.io",             "/jobs/",        "greenhouse"),
    ("jobs.lever.co",             "",              "lever"),
    ("lever.co",                  "",              "lever"),
    ("jobs.ashbyhq.com",          "",              "ashby"),
    ("ashbyhq.com",               "",              "ashby"),
    ("myworkdayjobs.com",         "",              "workday"),
    ("workday.com",               "",              "workday"),
    # ATSes without a dedicated adapter yet — route to "generic" so the
    # AgentLoop drives the form. Hosts that also serve marketing/company
    # content get a required-path filter (apply.workable.com/<co>/j/<id>,
    # jobs.smartrecruiters.com/<Co>/<id>); pure-ATS hosts
    # (ats.rippling.com, <co>.pinpointhq.com) match on host alone.
    ("apply.workable.com",        "/j/",           "generic"),
    ("workable.com",              "/j/",           "generic"),
    ("ats.rippling.com",          "",              "generic"),
    ("jobs.smartrecruiters.com",  "",              "generic"),
    ("pinpointhq.com",            "",              "generic"),
    ("linkedin.com",              "/jobs/view/",   "linkedin"),
)

# Hard-exclude patterns. Any URL whose path matches one of these is rejected
# even if its host is in _ATS_HOSTS. These are the noisy footer/social links
# that show up on RR (and many career-listing aggregators) and would
# otherwise capture the resolver before the real Apply link is reached.
_NON_APPLICATION_PATH_PATTERNS: tuple[str, ...] = (
    "/company/",   # linkedin.com/company/X
    "/in/",        # linkedin.com/in/X (person profile)
    "/school/",    # linkedin.com/school/X
    "/about",      # */about, */about-us
    "/login",
    "/signup",
    "/sign-in",
    "/contact",
    "/privacy",
    "/terms",
)


def _detect_ats_from_url(url: str) -> Optional[str]:
    """Map a URL to an ATS registry key, or None if it isn't a job application.

    Two-stage filter:
      1. Reject anything whose path is clearly NOT an application (company
         pages, profiles, login, etc.).
      2. Among the remainder, the host must be a known ATS AND, if that host
         is multi-purpose (linkedin.com, greenhouse.io), the path must also
         contain the host-specific job marker.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if not host:
        return None
    # Stage 1 — kill obvious non-application URLs early.
    for bad in _NON_APPLICATION_PATH_PATTERNS:
        if bad in path:
            return None
    # Stage 2 — host-matched + required path substring.
    for needle, required_path, key in _ATS_HOSTS:
        if needle in host:
            if required_path and required_path not in path:
                continue
            return key
    return None


class RemoteRocketshipAdapter(BasePlatformAdapter):
    platform_name = "remoterocketship"
    container_selector = None

    def __init__(self) -> None:
        self._inner: Optional[BasePlatformAdapter] = None
        self._resolved_url: Optional[str] = None
        # Mirror attributes the executor reads via getattr on the adapter.
        # These get populated from self._inner after navigation.
        self._iframe_mode: bool = False
        self._frame_locator = None
        self._frame = None

    # ──────────────────────────────────────────────────────────────────────
    # URL resolution
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    async def _scrape_apply_url(page: Page, rr_url: str) -> Optional[str]:
        """Load the RR listing in the page and extract the ATS application URL.

        We use the existing Playwright page (already has stealth, cookies, the
        real Chromium UA) because RR's CDN 403s plain httpx requests. The
        Apply CTA on RR is rendered as ``<button aria-label="Apply">`` with no
        href — the actual ATS link lives in another anchor on the page (e.g.
        a "View on company site" / "Apply on company website" link, or an
        ``<a target="_top">`` wrapping the Apply button). We collect every
        external anchor and pick the first one pointing at a known ATS host.
        """
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

        logger.warning(
            f"[RR] No ATS URL found in {rr_url!r} (scanned {len(hrefs or [])} anchors)"
        )
        return None

    # ──────────────────────────────────────────────────────────────────────
    # BasePlatformAdapter
    # ──────────────────────────────────────────────────────────────────────

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        # Lazy import to avoid a circular import with adapters/__init__.py.
        from .registry import get_adapter

        # 0. The pipeline sometimes stores the ALREADY-RESOLVED inner-ATS URL as
        # jobs.source_url while still tagging jobs.source='www.remoterocketship.com'
        # (so it routes here). If the URL we were handed is itself a known ATS
        # (not a remoterocketship.com listing), skip the RR scrape and delegate
        # straight to that ATS adapter — otherwise we'd try to scrape an
        # Ashby/Greenhouse page as if it were an RR listing and fail.
        direct_key = _detect_ats_from_url(job_url)
        hostname = (urlparse(job_url).hostname or "").lower()
        if direct_key and "remoterocketship" not in hostname and "remote100k" not in hostname:
            self._inner = self._spawn_delegate(direct_key)
            self._resolved_url = job_url
            logger.info(f"[RR] URL is already a resolved {direct_key!r} ATS — delegating directly")
            await self._inner.navigate_to_application(page, job_url)
            self._iframe_mode = getattr(self._inner, "_iframe_mode", False)
            self._frame_locator = getattr(self._inner, "_frame_locator", None)
            self._frame = getattr(self._inner, "_frame", None)
            return

        # 1. Load RR in the real browser, scrape the underlying ATS link.
        resolved = await self._scrape_apply_url(page, job_url)

        if resolved:
            ats_key = _detect_ats_from_url(resolved) or "generic"
            self._inner = self._spawn_delegate(ats_key)
            self._resolved_url = resolved
            logger.info(f"[RR] Delegating to {ats_key!r} adapter for {resolved!r}")
            await self._inner.navigate_to_application(page, resolved)
        else:
            # 2. Fallback — no recognizable ATS link. Hand the CURRENT page to
            # the generic adapter and run its navigate step: that clicks the
            # page's Apply button when no form is visible yet, which unknown
            # ATSes (e.g. Jobvite) need before any form exists to fill. Without
            # it the pipeline "fills" the job-description page and dies at
            # submit ("no submit button found").
            logger.info(f"[RR] No ATS link found on {job_url!r}; delegating current page to generic adapter")
            self._inner = self._spawn_delegate("generic")
            try:
                await self._inner.navigate_to_application(page, page.url)
            except Exception as exc:
                logger.warning(f"[RR] generic fallback navigation failed: {exc}")

        # Mirror the inner adapter's iframe state onto self so the executor's
        # getattr() reads (executor.py:249, 335) see the right values.
        self._iframe_mode = getattr(self._inner, "_iframe_mode", False)
        self._frame_locator = getattr(self._inner, "_frame_locator", None)
        self._frame = getattr(self._inner, "_frame", None)

    async def detect_application_type(self, page: Page) -> str:
        if self._inner:
            try:
                return await self._inner.detect_application_type(page)
            except NotImplementedError:
                pass
        return self.platform_name

    async def fill_application(
        self,
        page: Page,
        profile: dict,
        resume_path: str,
        cover_letter_path: Optional[str],
        screening_answers: Optional[dict],
        pre_detected_form=None,
        candidate_id: Optional[str] = None,
    ) -> bool:
        if not self._inner:
            return False
        return await self._inner.fill_application(
            page, profile, resume_path, cover_letter_path,
            screening_answers, pre_detected_form=pre_detected_form,
            candidate_id=candidate_id,
        )

    async def submit(self, page: Page) -> bool:
        if not self._inner:
            return False
        return await self._inner.submit(page)

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        if not self._inner:
            return (False, None)
        return await self._inner.verify_success(page)

    async def refresh_frame(self, page: Page) -> None:
        if self._inner and hasattr(self._inner, "refresh_frame"):
            await self._inner.refresh_frame(page)
            self._iframe_mode = getattr(self._inner, "_iframe_mode", False)
            self._frame_locator = getattr(self._inner, "_frame_locator", None)
            self._frame = getattr(self._inner, "_frame", None)
