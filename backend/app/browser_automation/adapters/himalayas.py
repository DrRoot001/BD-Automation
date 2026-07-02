"""Himalayas (himalayas.app) adapter — native Quick Apply OR ATS passthrough.

Himalayas is a remote-jobs board with two listing flavours:

  a. **Native Quick Apply** — a "Quick Apply" button opens a Himalayas-hosted
     form (name / email / resume, sometimes screening questions) that
     Himalayas forwards to the employer. Filled with the shared
     detect_form/fill_form helpers.

  b. **External ATS** — the Apply CTA links straight out to Greenhouse /
     Lever / Ashby / etc. We scan the page anchors, detect the ATS with the
     same ``_detect_ats_from_url`` used by RemoteRocketship, and DELEGATE
     every subsequent adapter method to the inner ATS adapter (RR
     passthrough pattern).

No auth wall and no captcha observed on Himalayas.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import Page

from .base import BasePlatformAdapter
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

_SUBMIT_SELECTORS = (
    "button[type='submit']",
    "button:has-text('Submit application')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit')",
)

_SUCCESS_TEXT_PATTERNS = (
    "application submitted",
    "thanks for applying",
    "thank you for applying",
    "application sent",
)


class HimalayasAdapter(BasePlatformAdapter):
    platform_name = "himalayas"
    container_selector = None

    def __init__(self) -> None:
        # Set when the listing delegates to an external ATS adapter.
        self._inner: Optional[BasePlatformAdapter] = None
        self._resolved_url: Optional[str] = None
        self._native_form: bool = False
        # Executor reads these via getattr — mirrored from the inner adapter.
        self._iframe_mode: bool = False
        self._frame_locator = None
        self._frame = None

    # ──────────────────────────────────────────────────────────────────────
    # Navigation
    # ──────────────────────────────────────────────────────────────────────

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        # Lazy import to avoid a circular import with registry.py (same
        # trick as remoterocketship.py / builtin.py).
        from .registry import get_adapter

        # 0. If the stored URL is already a resolved ATS URL (jobs.source is
        # just the 'himalayas' label), delegate straight away.
        direct_key = _detect_ats_from_url(job_url)
        hostname = (urlparse(job_url).hostname or "").lower()
        if direct_key and "himalayas" not in hostname:
            self._inner = get_adapter(direct_key)
            self._resolved_url = job_url
            logger.info(f"[Himalayas] URL is already a resolved {direct_key!r} ATS — delegating directly")
            await self._inner.navigate_to_application(page, job_url)
            self._mirror_inner_attrs()
            return

        logger.info(f"[Himalayas] Navigating to job {job_url!r}")
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        except Exception as exc:
            logger.warning(f"[Himalayas] job goto soft-failed ({exc}); settling page")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8_000)
            except Exception:
                pass
        await self.human_delay(0.8, 1.6)

        # 1. Native Quick Apply branch — click and drive the Himalayas form.
        for sel in _QUICK_APPLY_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=_FIELD_TIMEOUT_MS)
                self._native_form = True
                logger.info(f"[Himalayas] Quick Apply clicked via {sel!r} — native form path")
                # Give the form time to render (modal or in-page section).
                await self.human_delay(1.0, 2.0)
                return
            except Exception as exc:
                logger.debug(f"[Himalayas] Quick Apply candidate {sel!r} failed: {exc}")

        # 2. External branch — scan anchors for a known ATS and delegate,
        # exactly like the RemoteRocketship passthrough.
        try:
            hrefs = await page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".map(a => a.href).filter(h => h.startsWith('http'))"
            )
        except Exception as exc:
            logger.warning(f"[Himalayas] DOM scrape failed: {exc}")
            hrefs = []

        resolved = None
        for href in hrefs or []:
            if _detect_ats_from_url(href):
                resolved = href
                break

        if resolved:
            ats_key = _detect_ats_from_url(resolved) or "generic"
            self._inner = get_adapter(ats_key)
            self._resolved_url = resolved
            logger.info(f"[Himalayas] Delegating to {ats_key!r} adapter for {resolved!r}")
            await self._inner.navigate_to_application(page, resolved)
            self._mirror_inner_attrs()
            return

        # 3. Nothing matched — fall through to generic so the AgentLoop's
        # vision agent can salvage whatever is on-screen.
        logger.warning(
            f"[Himalayas] No Quick Apply button or ATS link found on {job_url!r} "
            f"(scanned {len(hrefs or [])} anchors); falling through to generic"
        )
        self._inner = get_adapter("generic")
        self._mirror_inner_attrs()

    def _mirror_inner_attrs(self) -> None:
        if not self._inner:
            return
        self._iframe_mode = getattr(self._inner, "_iframe_mode", False)
        self._frame_locator = getattr(self._inner, "_frame_locator", None)
        self._frame = getattr(self._inner, "_frame", None)

    async def detect_application_type(self, page: Page) -> str:
        if self._native_form:
            return "QUICK_APPLY"
        if self._inner:
            try:
                return await self._inner.detect_application_type(page)
            except NotImplementedError:
                pass
        return "EXTERNAL"

    # ──────────────────────────────────────────────────────────────────────
    # Fill / submit / verify — native form or delegation.
    # ──────────────────────────────────────────────────────────────────────

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
        if self._inner:
            return await self._inner.fill_application(
                page, profile, resume_path, cover_letter_path,
                screening_answers, pre_detected_form=pre_detected_form,
                candidate_id=candidate_id,
            )
        from ..forms import detect_form, fill_form

        form = pre_detected_form or await detect_form(page, container_selector=self.container_selector)
        return await fill_form(page, form, profile, screening_answers, candidate_id=candidate_id)

    async def submit(self, page: Page) -> bool:
        if self._inner:
            return await self._inner.submit(page)

        for sel in _SUBMIT_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=_FIELD_TIMEOUT_MS)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                await self.human_delay(1.0, 2.0)
                logger.info(f"[Himalayas] Submitted via {sel!r}")
                return True
            except Exception as exc:
                logger.debug(f"[Himalayas] submit selector {sel!r} failed: {exc}")
        logger.error("[Himalayas] No submit button matched")
        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        if self._inner:
            return await self._inner.verify_success(page)
        content = (await self._safe_content(page)).lower()
        for pattern in _SUCCESS_TEXT_PATTERNS:
            if pattern in content:
                logger.info(f"[Himalayas] Success verified via text {pattern!r}")
                return (True, pattern)
        return (False, None)

    async def refresh_frame(self, page: Page) -> None:
        if self._inner and hasattr(self._inner, "refresh_frame"):
            await self._inner.refresh_frame(page)
            self._mirror_inner_attrs()

    @staticmethod
    async def _safe_content(page: Page) -> str:
        try:
            return await page.content()
        except Exception:
            return ""
