"""Shared verification / captcha handlers for the autonomous adapters.

Previously every login/aggregator adapter grew its own OTP-fetch and captcha
helpers (`_maybe_solve_hcaptcha`, `_maybe_solve_turnstile`,
`_wait_for_recaptcha`, `_solve_interstitial_captcha`, …). Those all did the same
thing against the same two services. This module centralises them into two
factory functions the shared :class:`~..autonomous.AutonomousAgent` consumes, so
NO adapter reimplements verification or captcha decision logic.

  build_verification_handler(candidate_id, sender_hint) → handler | None
      Detects an emailed one-time-code screen, fetches the code from the
      candidate's Gmail (verification.code_fetcher) and types it in. Returns
      None when there's no candidate (nothing to fetch) so the agent halts with
      a clear OTP/EMAIL_VERIFICATION status instead of spinning.

  build_captcha_handler() → handler
      Detects the captcha kind on the page and hands it to the funded
      CaptchaService. Fails soft (returns False) so the agent halts with
      CAPTCHA_REQUIRED rather than raising.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# post-code advance buttons, most-specific first
_CODE_ADVANCE = (
    "button:has-text('Submit Code')", "button:has-text('Verify Code')",
    "button:has-text('Confirm Code')", "button:has-text('Confirm')",
    "button:has-text('Verify')", "button:has-text('Continue')",
    "button:has-text('Next')", "button:has-text('Submit')",
    "button[type='submit']",
)


def build_verification_handler(
    candidate_id: Optional[str], sender_hint: str = ""
) -> Optional[Callable]:
    """Return an async verification handler, or None if we can't fetch a code."""
    if not candidate_id:
        return None

    async def handler(page: Any, frame: Any, state: Any, cond: Any) -> bool:
        try:
            from ..verification import detect_code_input, fetch_verification_code, fill_code
        except Exception as exc:  # pragma: no cover - import guard
            logger.debug(f"[handlers] verification import failed: {exc}")
            return False

        try:
            shape = await detect_code_input(page, frame)
        except Exception:
            shape = None
        if not shape:
            # No code widget yet (email may be an "click the link" flow, or the
            # code screen hasn't rendered). Nothing to fill this turn.
            return False

        code = await fetch_verification_code(
            candidate_id, after_epoch=int(time.time()) - 600, sender_hint=sender_hint,
        )
        if not code:
            return False  # not delivered yet → agent re-observes / bounded wait

        ok = await fill_code(page, frame, shape, code)
        if not ok:
            return False

        # advance past the code screen
        ctx = frame or page
        for sel in _CODE_ADVANCE:
            try:
                btn = ctx.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=5_000)
                    break
            except Exception:
                continue
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        return True

    return handler


def _captcha_kind(state: Any) -> Optional[str]:
    hay = ((state.visible_text or "") + " " + (state.html or "")).lower()
    if "turnstile" in hay or "challenges.cloudflare" in hay:
        return "turnstile"
    if "hcaptcha" in hay:
        return "hcaptcha"
    if "recaptcha" in hay or "g-recaptcha" in hay:
        return "recaptcha_v2"
    if "captcha" in hay:
        return "image"
    return None


def build_captcha_handler(enabled: bool = True) -> Optional[Callable]:
    """Return an async captcha handler that routes to the shared CaptchaService."""
    if not enabled:
        return None

    async def handler(page: Any, frame: Any, state: Any) -> bool:
        kind = _captcha_kind(state)
        if not kind:
            return False
        try:
            from ..captcha import CaptchaService, resolve_captcha_provider
            svc = CaptchaService(provider=resolve_captcha_provider())
            solution = await svc.solve(page, kind)
            return bool(getattr(solution, "success", False))
        except Exception as exc:
            logger.warning(f"[handlers] captcha solve failed ({kind}): {exc}")
            return False

    return handler
