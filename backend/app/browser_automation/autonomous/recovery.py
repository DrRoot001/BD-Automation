"""RecoveryController — deterministic resilience reflexes for the autonomous loop.

The reasoner already re-plans every turn from fresh perception, which handles a
lot on its own (conditional fields, multi-step flows, validation errors it can
see). This module adds the CHEAP, DETERMINISTIC recovery reflexes that let the
agent *recover instead of failing immediately* on the common breakages:

    dismiss_blocking_ui   — cookie/consent overlays + non-application modals /
                            popups that cover the form
    recover_action        — a failed action (missing element, timeout, covered
                            target) → dismiss overlays + scroll-to-reveal + retry
    wait_for_load         — slow-loading / still-hydrating page → wait + re-observe
    recover_from_stall    — page stopped changing → scroll, dismiss, reload nudge
                            before giving up as STUCK

Everything is bounded (per-run budgets + per-action retry caps) so recovery can
never itself become an infinite loop. Every method is best-effort and never
raises out.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..reasoning.models import ActionType
from .models import ActionResult

logger = logging.getLogger(__name__)

# Cookie / consent / privacy overlays — accept or close them so they stop
# covering the form. Ordered vendor-specific → generic text.
_CONSENT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "#onetrust-reject-all-handler",
    ".onetrust-close-btn-handler",
    "#CybotCookiebotDialogBodyButtonAccept",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    ".osano-cm-accept-all",
    ".osano-cm-close",
    "#truste-consent-button",
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept All Cookies')",
    "button:has-text('I agree')",
    "button:has-text('Got it')",
    "button:has-text('Allow all')",
)

# Generic modal/popup close controls (used when perception didn't resolve one).
_MODAL_CLOSE_SELECTORS = (
    "[aria-label*='close' i]",
    "[aria-label*='dismiss' i]",
    "button[title*='close' i]",
    "button.close",
    ".close-button",
    "[class*='close-btn']",
    "[class*='modal'] button:has-text('×')",
    "button:has-text('No thanks')",
    "button:has-text('Maybe later')",
)

# Actions worth retrying after a recovery nudge.
_RETRYABLE = {
    ActionType.CLICK, ActionType.CLICK_APPLY, ActionType.FILL,
    ActionType.SELECT_OPTION, ActionType.UPLOAD, ActionType.NEXT_STEP,
    ActionType.SUBMIT, ActionType.NAVIGATE,
}


class RecoveryController:
    def __init__(
        self,
        *,
        max_action_retries: int = 2,
        dismiss_budget: int = 8,
        loading_wait_budget: int = 4,
        stall_recovery_budget: int = 2,
    ):
        self.max_action_retries = max_action_retries
        self._dismiss_budget = dismiss_budget
        self._loading_budget = loading_wait_budget
        self._stall_budget = stall_recovery_budget
        self._action_retries: dict[str, int] = {}
        self._dismissed: set[str] = set()

    # ── budgets ───────────────────────────────────────────────────────────────
    def can_dismiss(self) -> bool:
        return self._dismiss_budget > 0

    def can_wait_for_load(self) -> bool:
        return self._loading_budget > 0

    def can_recover_stall(self) -> bool:
        return self._stall_budget > 0

    def note_progress(self) -> None:
        """The page changed → recovery succeeded / isn't needed. Reset the
        per-action retry ledger so a later, unrelated failure gets its own budget."""
        self._action_retries.clear()

    # ── overlay / modal / popup dismissal ─────────────────────────────────────
    async def dismiss_blocking_ui(self, page: Any, frame: Any, state: Any) -> Optional[str]:
        """Close a cookie/consent overlay or a non-application modal covering the
        form. Returns a short description of what was dismissed, or None. Overlays
        are always page-level (even when the form is in an iframe)."""
        if not self.can_dismiss():
            return None
        # 1. consent / cookie overlays
        for sel in _CONSENT_SELECTORS:
            hit = await self._click_if_visible(page, sel)
            if hit:
                self._dismiss_budget -= 1
                logger.info(f"[Recovery] dismissed consent overlay via {sel!r}")
                return f"consent:{sel}"
        # 2. non-application modals surfaced by perception (use their close btn)
        for m in (getattr(state, "modals", None) or []):
            if getattr(m, "looks_like_application", False):
                continue
            key = m.selector or ""
            if key in self._dismissed:
                continue
            target = m.close_selector
            if target and await self._click_if_visible(page, target):
                self._dismissed.add(key)
                self._dismiss_budget -= 1
                logger.info(f"[Recovery] closed modal {key!r} via {target!r}")
                return f"modal:{key}"
        # 3. generic close controls (perception found a modal but no close btn)
        if getattr(state, "modals", None):
            non_app = [m for m in state.modals if not getattr(m, "looks_like_application", False)]
            if non_app:
                for sel in _MODAL_CLOSE_SELECTORS:
                    if await self._click_if_visible(page, sel):
                        self._dismiss_budget -= 1
                        logger.info(f"[Recovery] closed a modal via generic {sel!r}")
                        return f"modal-generic:{sel}"
        return None

    # ── failed-action recovery ────────────────────────────────────────────────
    async def recover_action(
        self, page: Any, frame: Any, state: Any, action: Any, executor: Any, context: Any,
    ) -> Optional[ActionResult]:
        """A just-executed action failed (missing element / timeout / covered).
        Try to make it succeed: dismiss overlays, scroll the target into view,
        then retry — bounded per action. Returns a fresh ActionResult, or None
        when the retry budget for this action is spent."""
        if action.type not in _RETRYABLE:
            return None
        sig = f"{action.type.value}:{(action.selector or action.value or action.url or '')[:60]}"
        used = self._action_retries.get(sig, 0)
        if used >= self.max_action_retries:
            return None
        self._action_retries[sig] = used + 1

        # a0) a transient dropdown/autocomplete/date-picker menu may be hanging
        #     OPEN and intercepting pointer events (Ashby location suggestion list,
        #     react-select menu). Those aren't consent/modal overlays, so Escape —
        #     not a close button — is what dismisses them. Cheap and safe: on a
        #     committed react-select it only closes the menu, it doesn't clear the
        #     value.
        await self._press_escape(page)
        # a) an overlay may be intercepting the click — clear it.
        await self.dismiss_blocking_ui(page, frame, state)
        # b) the element may be below the fold or lazy — scroll it in.
        ctx = frame or page
        if action.selector:
            try:
                await ctx.locator(action.selector).first.scroll_into_view_if_needed(timeout=2_000)
            except Exception:
                # fall back to scrolling the page so lazy content renders
                try:
                    await page.evaluate("window.scrollBy(0, 400)")
                except Exception:
                    pass
        await asyncio.sleep(0.4)
        logger.info(f"[Recovery] retrying {sig} (attempt {used + 1}/{self.max_action_retries})")
        try:
            return await executor.execute(page, frame, action, context)
        except Exception as exc:  # pragma: no cover - executor already guards
            return ActionResult(ok=False, error=str(exc), note="retry raised")

    # ── slow-loading recovery ─────────────────────────────────────────────────
    @staticmethod
    def page_looks_unready(state: Any) -> bool:
        """True when the observed page has nothing actionable AND looks like it is
        still loading — the cue to wait rather than reason on a blank shell."""
        if getattr(state, "error", None):
            return True
        if state.has_form or state.buttons or state.success_messages or state.errors or state.modals:
            return False
        text_len = len((state.visible_text or "").strip())
        return (not state.is_stable) or text_len < 20

    async def wait_for_load(self, page: Any) -> bool:
        """Wait for a slow page to settle. Returns True if we spent budget."""
        if not self.can_wait_for_load():
            return False
        self._loading_budget -= 1
        for st in ("domcontentloaded", "networkidle"):
            try:
                await page.wait_for_load_state(st, timeout=3_000)
            except Exception:
                pass
        await asyncio.sleep(0.6)
        return True

    # ── stall recovery ────────────────────────────────────────────────────────
    async def recover_from_stall(self, page: Any, frame: Any, state: Any) -> bool:
        """Last-ditch nudge before declaring STUCK: dismiss overlays, scroll the
        page (reveals lazy content), and settle. Returns True if it likely changed
        the page (so the caller resets the stuck counter and re-observes)."""
        if not self.can_recover_stall():
            return False
        self._stall_budget -= 1
        changed = False
        # Close any stuck-open dropdown/autocomplete overlay first (Escape) — the
        # #1 cause of a mutating-action stall is a suggestion menu blocking clicks.
        if await self._press_escape(page):
            changed = True
        if await self.dismiss_blocking_ui(page, frame, state):
            changed = True
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.4)
            await page.evaluate("window.scrollTo(0, 0)")
            changed = True
        except Exception:
            pass
        await self.wait_for_load(page)
        logger.info(f"[Recovery] stall recovery ran (changed={changed})")
        return changed

    # ── helper ────────────────────────────────────────────────────────────────
    @staticmethod
    async def _press_escape(page: Any) -> bool:
        """Press Escape to dismiss a transient open overlay (react-select menu,
        autocomplete suggestion list, date picker). Best-effort; never raises."""
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            return True
        except Exception:
            return False

    @staticmethod
    async def _click_if_visible(page: Any, selector: str) -> bool:
        try:
            loc = page.locator(selector).first
            if await loc.count() == 0 or not await loc.is_visible():
                return False
            await loc.click(timeout=1_500)
            await asyncio.sleep(0.25)
            return True
        except Exception:
            return False
