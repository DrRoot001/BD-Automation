"""Ashby (jobs.ashbyhq.com) adapter.

Ashby is React-heavy with custom select widgets. URL pattern::

    https://jobs.ashbyhq.com/<company>/<job-id>
    https://jobs.ashbyhq.com/<company>/<job-id>/application

Some companies embed Ashby via an iframe (`#ashby_embed_iframe`) on their own
careers page; we detect that the same way Greenhouse does.

The "Apply" CTA on the job-description page navigates to /application. The
form fields use Ashby's custom React widgets (custom dropdowns, file pickers
that POST to an upload endpoint then attach the URL to a hidden input).
The detector + filler already handle `custom_widget=True` selects.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Tuple

from playwright.async_api import Frame, Page

from .base import BasePlatformAdapter
from ..agent import get_learned_fixes
from ..forms import detect_form, fill_form, upload_file

logger = logging.getLogger(__name__)

_IFRAME_SEL = "#ashby_embed_iframe, iframe[src*='ashbyhq']"

_APPLY_SELECTORS = [
    "a[href*='/application']",
    "a:has-text('Apply for this Job')",
    "a:has-text('Apply for this job')",
    "button:has-text('Apply for this Job')",
    "button:has-text('Apply Now')",
    "a:has-text('Apply')",
    ".ashby-job-posting-apply-button",
]

_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "button:has-text('Submit Application')",
    "button:has-text('Submit application')",
    "button:has-text('Submit')",
    ".ashby-application-submit-button",
]

_SUCCESS_PATTERNS = (
    "application received",
    "you've applied",
    "you have applied",
    "thank you for applying",
    "we've received your application",
)

# Ashby returns a 200 OK with a red banner instead of an HTTP error when its
# anti-bot scores the submission as automated. The banner is server-rendered,
# so checking page content reliably catches it. Detecting this lets us:
#   1. Mark the application with a specific SPAM_FLAGGED failure_reason
#      (so retries don't re-submit and escalate the flag to IP block),
#   2. Surface a meaningful message to the operator instead of generic FAILED.
_SPAM_PATTERNS = (
    "flagged as possible spam",
    "couldn't submit your application",
    "could not submit your application",
    "submission was flagged",
    "we were unable to submit",
)

# Ashby shows this instead of the normal form when the candidate (by email)
# has already submitted an application for this job. This is NOT a failure —
# it's a distinct, expected outcome — so it must be classified separately
# rather than falling through to a generic FAILED that Celery would retry
# (retrying would just hit the same wall every attempt).
_ALREADY_APPLIED_PATTERNS = (
    "already applied",
    "already submitted an application",
    "you've already applied",
    "you have already applied",
    "already have an application on file",
)


class AshbyAdapter(BasePlatformAdapter):
    platform_name = "ashby"
    container_selector = None  # Ashby form is full page; no stable wrapper id

    def __init__(self):
        self._iframe_mode: bool = False
        self._frame: Optional[Frame] = None

    @staticmethod
    def _build_application_url(job_url: str) -> str:
        """Append '/application' to the PATH, not the raw URL string.

        Only applies on the canonical jobs.ashbyhq.com board host, where the
        <job-id>/application route convention is known to hold. Custom-domain
        or query-param-addressed pages (e.g. Ashby's own www.ashbyhq.com/
        careers?ashby_jid=<uuid>, or a company's bespoke careers site) use a
        completely different routing model — for those, blindly concatenating
        "/application" onto the end of the URL string lands AFTER the query
        string (?ashby_jid=<uuid>/application), corrupting the job id and
        producing a "Job not found" page. For any non-canonical host, return
        the URL unchanged and let the existing Apply-button detection below
        (plus the vision PageAgent and AgentLoop's own click_apply retries)
        find and click whatever the real application entry point is.
        """
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(job_url)
        host = (parsed.hostname or "").lower()
        if host != "jobs.ashbyhq.com":
            return job_url
        path = parsed.path.rstrip("/")
        if path.endswith("/application"):
            return job_url
        return urlunparse(parsed._replace(path=path + "/application"))

    async def _wait_for_job_content(self, page: Page, timeout_ms: int = 15_000) -> None:
        """Poll until the page shows real job content, not just the site shell.

        Custom-domain Ashby pages (e.g. Ashby's own careers page addressed by
        a query-string job id) fetch job details asynchronously AFTER the
        initial shell is interactive — `networkidle` can fire well before
        that fetch resolves, leaving the page looking like a generic "no job
        here" shell (nav/footer only) when the real posting + Apply button
        genuinely exist a couple seconds later. Poll for body text actually
        containing "apply" and having grown past a trivial length, rather
        than trusting one fixed delay. Exits immediately once real content
        shows up; degrades to a no-op if content never grows (genuinely thin
        page — the existing Apply-detection below will report that honestly).
        """
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        last_len = -1
        stable_count = 0
        while time.monotonic() < deadline:
            try:
                text_len = await page.evaluate("() => (document.body.innerText || '').length")
                has_apply = await page.evaluate(
                    "() => /apply/i.test(document.body.innerText || '')"
                )
            except Exception:
                break
            if has_apply and text_len > 500:
                logger.info(f"[Ashby] job content hydrated (text_len={text_len})")
                return
            stable_count = stable_count + 1 if text_len == last_len else 0
            last_len = text_len
            if stable_count >= 4:  # content stopped growing for ~1.2s, still no Apply text
                break
            await asyncio.sleep(0.3)

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        # If on the job description page, prefer /application directly
        # (canonical host only — see _build_application_url).
        target = self._build_application_url(job_url)
        is_canonical_rewrite = target != job_url
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=25_000)
        except Exception as exc:
            logger.warning(f"[Ashby] /application navigate failed ({exc}); retrying base URL")
            try:
                await page.goto(job_url, wait_until="domcontentloaded", timeout=25_000)
            except Exception:
                pass

        # Non-canonical hosts (custom domains, query-param-addressed pages)
        # may still be hydrating the actual job content — see
        # _wait_for_job_content. Canonical jobs.ashbyhq.com pages already
        # navigate straight to a job-specific path and don't need this.
        if not is_canonical_rewrite:
            try:
                await self._wait_for_job_content(page)
            except Exception as exc:
                logger.debug(f"[Ashby] job-content wait failed (non-fatal): {exc}")

        # Detect iframe embed
        self._iframe_mode = False
        self._frame = None
        try:
            await page.wait_for_selector(_IFRAME_SEL, timeout=4_000)
            for fr in page.frames:
                if "ashbyhq" in (fr.url or ""):
                    self._frame = fr
                    self._iframe_mode = True
                    logger.info(f"[Ashby] iframe mode (frame={fr.url})")
                    try:
                        await fr.wait_for_load_state("domcontentloaded", timeout=10_000)
                    except Exception:
                        pass
                    break
        except Exception:
            pass

        target_ctx = self._frame if self._iframe_mode else page

        # If still no inputs visible, click Apply
        try:
            await target_ctx.locator("input, textarea, select").first.wait_for(state="attached", timeout=3_000)
        except Exception:
            learned = get_learned_fixes("ashby").get("apply_button")
            for sel in learned + _APPLY_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.scroll_into_view_if_needed()
                        await btn.click(timeout=5_000)
                        await page.wait_for_load_state("domcontentloaded", timeout=12_000)
                        get_learned_fixes("ashby").add("apply_button", sel)
                        logger.info(f"[Ashby] Apply clicked via {sel!r}")
                        break
                except Exception:
                    continue

        await self.human_delay(0.5, 1.5)

    async def detect_application_type(self, page: Page) -> str:
        return "EXTERNAL_FORM"

    async def fill_application(
        self,
        page: Page,
        profile: dict,
        resume_path: str,
        cover_letter_path: Optional[str],
        screening_answers: Optional[dict],
        pre_detected_form=None, candidate_id=None,
    ) -> bool:
        ctx = self._frame if self._iframe_mode else page
        form = pre_detected_form or await detect_form(ctx, container_selector=self.container_selector)
        fill_success = await fill_form(ctx, form, profile, screening_answers, candidate_id=candidate_id)

        for field in form.fields:
            if field.field_type != "file":
                continue
            lbl = (field.label or "").lower()
            if cover_letter_path and "cover" in lbl:
                await upload_file(ctx, field.selector, cover_letter_path)
            elif "resume" in lbl or "cv" in lbl or not any(k in lbl for k in ("cover", "other")):
                await upload_file(ctx, field.selector, resume_path)

        return fill_success

    async def submit(self, page: Page) -> bool:
        ctx = self._frame if self._iframe_mode else page
        # Pre-submit dwell — gives Ashby's anti-bot a "user reading the form"
        # window between the last keystroke and the submit click. Sub-second
        # gaps fire their automation heuristic. Configurable via
        # ASHBY_PRE_SUBMIT_DWELL_MS (default 4000).
        import asyncio as _asyncio
        import os as _os
        _dwell_ms = int(_os.getenv("ASHBY_PRE_SUBMIT_DWELL_MS", "4000"))
        if _dwell_ms > 0:
            logger.info(f"[Ashby] pre-submit dwell {_dwell_ms}ms (anti-spam)")
            await _asyncio.sleep(_dwell_ms / 1000.0)
        learned = get_learned_fixes("ashby").get("submit")
        for sel in learned + [s for s in _SUBMIT_SELECTORS if s not in learned]:
            try:
                btn = ctx.locator(sel).first
                if await btn.count() > 0:
                    # Ashby keeps the submit button disabled until all required
                    # fields (and the resume upload) are complete. Clicking a
                    # disabled button is a silent no-op — Playwright won't
                    # error, the page just doesn't advance, and the caller
                    # would wrongly believe a submit attempt happened. Check
                    # both the native `disabled` attribute and `aria-disabled`
                    # (Ashby's custom button component uses the latter).
                    is_disabled = await btn.evaluate(
                        "el => el.disabled === true || el.getAttribute('aria-disabled') === 'true'"
                    )
                    if is_disabled:
                        logger.warning(
                            f"[Ashby] submit button {sel!r} found but DISABLED — "
                            "form likely has an incomplete required field or a "
                            "still-uploading resume. Skipping click, trying next "
                            "selector / letting caller re-check required fields."
                        )
                        continue
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=6_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=12_000)
                    except Exception:
                        pass
                    await self.human_delay(1.0, 2.0)
                    get_learned_fixes("ashby").add("submit", sel)
                    logger.info(f"[Ashby] submit via {sel!r}")
                    return True
            except Exception as exc:
                logger.debug(f"[Ashby] submit selector {sel!r} failed: {exc}")
        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        # Spam-flag and already-applied checks run FIRST — Ashby returns 200
        # with a banner instead of a non-2xx for both, so without these we'd
        # fall through to "generic FAILED" and the retry handler would
        # re-submit: escalating the anti-bot flag in the spam case, or just
        # hitting the same "already applied" wall every retry in the other.
        for ctx in ([self._frame, page] if self._iframe_mode else [page]):
            if ctx is None:
                continue
            try:
                content = (await ctx.content()).lower()
            except Exception:
                continue
            for pattern in _ALREADY_APPLIED_PATTERNS:
                if pattern in content:
                    logger.warning(
                        f"[Ashby] ALREADY_APPLIED — candidate has an existing "
                        f"application on file (pattern={pattern!r}). Not a "
                        "failure; do not retry."
                    )
                    raise Exception(
                        "ALREADY_APPLIED: Ashby indicates this candidate has "
                        "already applied to this job. This is an expected "
                        "outcome, not a failure — no retry needed."
                    )
            for pattern in _SPAM_PATTERNS:
                if pattern in content:
                    logger.error(
                        f"[Ashby] SPAM_FLAGGED — server rejected submission "
                        f"(pattern={pattern!r}). Surfacing as terminal failure; "
                        "retrying would escalate the IP-level flag."
                    )
                    # Raise a sentinel-string exception the executor + Celery
                    # task already recognise as a no-retry terminal failure.
                    raise Exception(
                        "SPAM_FLAGGED: Ashby anti-bot rejected the submission "
                        "(\"flagged as possible spam\"). Likely causes: form was "
                        "partially filled or submit fired too fast. Do NOT retry "
                        "immediately — wait or run from a different IP."
                    )
            for pattern in _SUCCESS_PATTERNS:
                if pattern in content:
                    return True, pattern
        return False, None
