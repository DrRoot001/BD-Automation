"""AutonomousAdapter — the shared base every platform adapter builds on.

The old model gave each adapter its own ``fill_application`` / ``submit`` /
``verify_success`` / ``detect_application_type`` plus a pile of duplicated
helpers (``_fill_first``, ``_click_first``, ``_wait_visible``,
``_dismiss_overlays``, ``_safe_content``) and hard-coded selector / success-
pattern lists. Every one of those re-implemented the same decision logic.

This base replaces ALL of it with the shared perception → reasoning → action
loop (:class:`~..autonomous.AutonomousAgent`). An adapter now supplies only what
is genuinely platform-specific, via small hooks:

    _resolve_target_url(page, job_url)  → where to actually navigate (URL rewrite,
                                          aggregator inner-ATS resolution). Return
                                          None to skip the goto (a hook did it).
    prepare(page)                        → deterministic pre-loop steps that the
                                          reasoner cannot do: authentication,
                                          following an external-apply redirect,
                                          account-owner default cleanup.
    _resolve_frame(page)                 → scope the loop to a form iframe.

Selectors, quirks and success signals live in ``hints.py`` and are fed to the
reasoner as :class:`BrowserMemory` — they are *hints*, not control flow.

Everything else (which field to fill, when to submit, whether it succeeded,
OTP/captcha/duplicate/MFA handling) is decided by the shared loop from observed
evidence. Adapters MUST NOT re-add workflow assumptions.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import Page

from .base import BasePlatformAdapter
from .handlers import build_captcha_handler, build_verification_handler
from ..autonomous import AgentStatus, AutonomousAgent

logger = logging.getLogger(__name__)


class AutonomousAdapter(BasePlatformAdapter):
    platform_name = "generic"
    container_selector: Optional[str] = None

    # Declarative config — override in subclasses.
    hints_key: Optional[str] = None        # hints.py key; defaults to platform_name
    iframe_selector: Optional[str] = None  # if set, the loop is scoped to this iframe
    login_gated: bool = False              # advertises that prepare() may sign in
    enable_captcha_handler: bool = True
    # Opt-in universal form discovery (agent/form_discoverer.py). OFF by default:
    # an adapter with its own prepare() already knows how to reveal its form, and
    # letting the discoverer also hunt for an Apply CTA there would add a click to
    # a battle-tested flow (these adapters land real SUBMITTEDs today). Turned ON
    # for GenericFormAdapter — the unknown-portal case, which has no prepare() and
    # is exactly where "find the form yourself" is the whole point.
    allow_form_discovery: bool = False

    def __init__(self, agent: Optional[AutonomousAgent] = None):
        self._agent = agent
        self._result = None               # AgentRunResult from the last run
        self._job_url: str = ""
        self._resolved_url: Optional[str] = None
        # iframe scoping state the executor reads via getattr.
        self._frame = None
        self._frame_locator = None
        self._iframe_mode = False
        # Last FormDiscoverer result (unknown portals only) — kept for logging
        # and so callers can see WHY a page was judged form-less.
        self._discovered_form = None

    # ── agent (lazy, per-adapter-instance) ────────────────────────────────────
    def _get_agent(self) -> AutonomousAgent:
        if self._agent is None:
            self._agent = AutonomousAgent()
        return self._agent

    # ─────────────────────────────────────────────────────────────────────────
    # Contract (shared — do NOT override in subclasses)
    # ─────────────────────────────────────────────────────────────────────────
    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        self._job_url = job_url
        target = await self._resolve_target_url(page, job_url)
        if target:
            self._resolved_url = target
            await self._goto(page, target)
        await self.prepare(page)
        await self._resolve_frame(page)

    async def detect_application_type(self, page: Page) -> str:
        """One observation — no workflow assumption."""
        try:
            state = await self._get_agent().collector.collect(page, self._frame, screenshot=False)
            if state.success_messages:
                return "EMAIL"
            return "EXTERNAL_FORM" if state.has_form else "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    async def fill_application(
        self,
        page: Page,
        profile: dict,
        resume_path: str,
        cover_letter_path: Optional[str],
        screening_answers: Optional[dict],
        pre_detected_form=None,
        candidate_id=None,
    ) -> bool:
        """Run the shared observe → reason → act loop over the application."""
        objective = self._build_objective(profile, screening_answers)
        context = {
            "resume_path": resume_path or getattr(self, "resume_local_path", None),
            "cover_letter_path": cover_letter_path or getattr(self, "cover_letter_local_path", None),
            "secrets": {"password": (getattr(self, "_candidate_credentials", {}) or {}).get("password", "")},
        }
        cid = candidate_id or getattr(self, "candidate_id", None)
        self._result = await self._get_agent().run(
            page,
            objective,
            frame=self._frame or self._frame_locator,
            browser_memory=self._build_browser_memory(),
            context=context,
            stop_before_submit=os.getenv("DRY_RUN_NO_SUBMIT", "false").lower() == "true",
            verification_handler=build_verification_handler(cid, sender_hint=self._sender_hint()),
            captcha_handler=build_captcha_handler(self.enable_captcha_handler),
        )
        logger.info(f"[{self.platform_name}] autonomous run -> {self._result.summary()}")
        return self._result.status.is_success

    async def submit(self, page: Page) -> bool:
        """The loop already submitted (or stopped for dry-run). Never re-click —
        a second submit would file a duplicate application."""
        return bool(self._result and self._result.status.is_success)

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        if self._result is None:
            return False, None
        if self._result.status == AgentStatus.SUBMITTED:
            return True, (self._result.confirmation or "submitted")
        return False, (self._result.error or self._result.status.value)

    async def refresh_frame(self, page: Page) -> None:
        try:
            await self._resolve_frame(page)
        except Exception as exc:
            logger.debug(f"[{self.platform_name}] refresh_frame: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # Hooks (override only what is genuinely platform-specific)
    # ─────────────────────────────────────────────────────────────────────────
    async def _resolve_target_url(self, page: Page, job_url: str) -> Optional[str]:
        """Where to navigate. Default: the job URL as-is. Override for URL
        rewrites (Lever /apply) or aggregator inner-ATS resolution. Return None
        to skip the goto entirely (a hook has already navigated)."""
        return job_url

    async def _goto(self, page: Page, url: str) -> None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        except Exception as exc:
            # Do NOT assume the page is dead — the loop's first OBSERVE classifies it.
            logger.warning(f"[{self.platform_name}] navigate warning (loop re-observes): {exc}")
        await self.human_delay(0.5, 1.2)

    async def prepare(self, page: Page) -> None:
        """Deterministic pre-loop steps the reasoner cannot do (auth, redirects).
        Default: nothing."""
        return None

    async def _resolve_frame(self, page: Page) -> None:
        """Scope the loop to the form's iframe.

        A declared ``iframe_selector`` (a known ATS) is authoritative and used
        as-is. When there ISN'T one — the unknown-portal case, including
        ``GenericFormAdapter`` — we DISCOVER it instead of giving up: without
        this, a never-seen portal that renders its form in an iframe is
        invisible to the loop, which scopes to the page, sees no fields and
        fails. Discovery is deterministic and LLM-free (see
        ``agent/form_discoverer.py``).
        """
        if not self.iframe_selector:
            if self._form_discovery_enabled():
                await self._discover_frame(page)
            if not self.iframe_selector:
                return
        try:
            loc = page.frame_locator(self.iframe_selector)
            # touch it so a missing iframe degrades to page-level rather than hanging
            await page.locator(self.iframe_selector).first.wait_for(state="attached", timeout=6_000)
            self._frame_locator = loc
            self._iframe_mode = True
            logger.info(f"[{self.platform_name}] scoped to iframe {self.iframe_selector!r}")
            # Wait through any destroy/recreate swap (Greenhouse re-injects the
            # iframe after load) until the FORM CONTENT inside settles — element
            # attach alone isn't enough, the frame may still be mid-reload.
            try:
                from ..frame_utils import wait_for_stability
                await wait_for_stability(page, loc, True)
            except Exception:
                pass
        except Exception:
            self._frame = None
            self._frame_locator = None
            self._iframe_mode = False

    def _form_discovery_enabled(self) -> bool:
        """Whether to run universal form discovery for this adapter.

        ``FORM_DISCOVERY_ALL_ADAPTERS=true`` forces it on everywhere — for
        deliberate testing only, since it changes proven adapters' behaviour.
        """
        import os

        if os.getenv("FORM_DISCOVERY_ALL_ADAPTERS", "false").lower() == "true":
            return True
        return self.allow_form_discovery

    async def _discover_frame(self, page: Page) -> None:
        """Find the application form on a portal we have no adapter knowledge for.

        Sets ``self.iframe_selector`` (instance attribute, so the class default
        is untouched) when the form turns out to live in an iframe. Leaves it
        unset when the form is page-level or nothing was found — both mean
        "run page-scoped", which is the existing behaviour.

        Best-effort by construction: any failure leaves the adapter exactly as
        it was, so this can only add reach, never take it away.
        """
        self._frame = None
        self._frame_locator = None
        self._iframe_mode = False
        try:
            from ..agent.form_discoverer import FormDiscoverer

            loc = await FormDiscoverer.discover(page)
        except Exception as exc:
            logger.debug(f"[{self.platform_name}] form discovery skipped (non-fatal): {exc}")
            return

        self._discovered_form = loc
        logger.info(f"[{self.platform_name}] form discovery: {loc.summary()}")
        if not loc.found:
            logger.warning(
                f"[{self.platform_name}] no application form found on this page — "
                f"evidence: {', '.join(loc.evidence) or '(none)'}"
            )
            return
        if loc.where == "iframe" and loc.iframe_selector:
            # Instance-level only: never mutate the class attribute, or one run
            # would leak its discovered selector into every later adapter of the
            # same type in this worker process.
            self.iframe_selector = loc.iframe_selector

    # ─────────────────────────────────────────────────────────────────────────
    # Shared helpers
    # ─────────────────────────────────────────────────────────────────────────
    def _sender_hint(self) -> str:
        return (self.hints_key or self.platform_name or "").lower()

    def _build_objective(self, profile: Optional[dict], screening: Optional[dict]) -> str:
        p = profile or {}
        name = p.get("name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip() or "the candidate"
        lines = [
            f"Complete and submit this job application on behalf of {name}.",
            "Fill each field with the candidate's real data — never invent values.",
            "CANDIDATE:",
            f"  name: {name}",
            f"  email: {p.get('email','')}",
            f"  phone: {p.get('phone','')}",
            f"  location: {p.get('location','')}",
        ]
        if p.get("current_title"):
            lines.append(f"  current role: {p.get('current_title')} at {p.get('current_company','')}")
        if self.login_gated:
            lines.append(
                "This portal may require signing in; the candidate HAS an account. "
                "Use email + password (never create a new account, never SSO)."
            )
        if screening:
            lines.append("PRE-RESOLVED SCREENING ANSWERS (use verbatim when a label matches):")
            for q, a in list(screening.items())[:20]:
                lines.append(f"  Q: {str(q)[:100]}  A: {str(a)[:160]}")
        lines.append("A tailored resume and cover letter are available to upload.")
        return "\n".join(l for l in lines if l.rstrip(": ").strip())

    def _build_browser_memory(self):
        """Project this platform's hints.py cheat-sheet into BrowserMemory so the
        reasoner starts from known selectors/quirks instead of rediscovering them.
        Hints are advisory context — never control flow."""
        try:
            from ..reasoning import BrowserMemory
            from .hints import get_platform_hints

            host = (urlparse(self._resolved_url or self._job_url).hostname or "") if (self._resolved_url or self._job_url) else ""
            hints = {}
            try:
                hints = get_platform_hints(self.hints_key or self.platform_name) or {}
            except Exception:
                hints = {}
            working = {}
            if hints.get("apply_selectors"):
                working["apply_button"] = list(hints["apply_selectors"])[:6]
            if hints.get("submit_selectors"):
                working["submit"] = list(hints["submit_selectors"])[:6]
            notes = list(hints.get("quirks") or [])
            if hints.get("url_hint"):
                notes.append(hints["url_hint"])
            return BrowserMemory(
                host=host,
                working_selectors=working,
                success_signal="; ".join(hints.get("success_patterns") or [])[:200],
                login_required=self.login_gated or None,
                notes=notes[:10],
            )
        except Exception as exc:
            logger.debug(f"[{self.platform_name}] browser_memory build skipped: {exc}")
            return None
