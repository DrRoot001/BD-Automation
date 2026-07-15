"""BrowserStateCollector — the single reusable perception entry point.

Every adapter (and eventually the agent loop) collects the current page into one
:class:`~.models.BrowserState` via this collector, instead of each re-scraping
the DOM its own way. The collector:

  * optionally waits for the page/frame to become *stable* (DOM quiescence +
    ``readyState === 'complete'``) so it never perceives a half-rendered SPA;
  * runs ONE in-page sweep (:data:`.scripts.EXTRACT_STATE_JS`) that returns
    forms, inputs, buttons, messages, modals, visible text and navigation state;
  * captures a screenshot through a configurable pipeline;
  * assembles a typed :class:`BrowserState`.

It carries **no business logic** — it observes and reports, nothing else. It is
frame-aware (pass the form iframe as ``frame``) and never raises on a normal
page: a capture failure yields a ``BrowserState`` with ``error`` set and
whatever partial data was gathered.

Usage::

    collector = BrowserStateCollector()
    state = await collector.collect(page)                 # top-level page
    state = await collector.collect(page, frame=form_frame)  # iframe-scoped

    if state.validation_errors:
        ...
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from playwright.async_api import Frame, Page

from ..frame_utils import get_live_frame
from .models import (
    BrowserState,
    ButtonElement,
    FormElement,
    InputField,
    ModalElement,
    NavigationState,
    PageMessage,
)
from .scripts import EXTRACT_STATE_JS, WAIT_STABLE_JS

logger = logging.getLogger(__name__)


@dataclass
class ScreenshotConfig:
    """Screenshot pipeline settings.

    Defaults (JPEG q45, full page) match the agent loop's economical vision
    turns; callers wanting an archival PNG can pass ``fmt="png"``.
    """
    enabled: bool = True
    fmt: str = "jpeg"          # "jpeg" | "png"
    quality: int = 45          # ignored for png
    full_page: bool = True
    timeout_ms: int = 15_000


@dataclass
class StabilityConfig:
    """DOM-quiescence detection settings."""
    quiet_ms: int = 500        # no-mutation window that counts as settled
    timeout_ms: int = 6_000    # hard ceiling before we give up waiting
    # Try to also reach network-idle first (best-effort; SPAs may never idle).
    wait_network_idle: bool = True
    network_idle_timeout_ms: int = 4_000


@dataclass
class ExtractCaps:
    """Upper bounds so a pathological page can't produce an unbounded payload."""
    max_fields: int = 300
    max_buttons: int = 120
    max_messages: int = 50
    max_modals: int = 12
    html_max_len: int = 500_000
    text_max_len: int = 20_000


class BrowserStateCollector:
    def __init__(
        self,
        screenshot: Optional[ScreenshotConfig] = None,
        stability: Optional[StabilityConfig] = None,
        caps: Optional[ExtractCaps] = None,
    ):
        self.screenshot_cfg = screenshot or ScreenshotConfig()
        self.stability_cfg = stability or StabilityConfig()
        self.caps = caps or ExtractCaps()

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────
    async def collect(
        self,
        page: Page,
        frame: Optional[Any] = None,
        *,
        screenshot: Optional[bool] = None,
        wait_stable: bool = True,
    ) -> BrowserState:
        """Collect the current page/frame into a :class:`BrowserState`.

        Args:
            page: the Playwright page. The screenshot is always taken here (a
                frame cannot screenshot itself).
            frame: an optional form iframe (Frame or FrameLocator). When given,
                DOM extraction runs INSIDE the frame; ``from_frame`` is set.
            screenshot: override the configured ``ScreenshotConfig.enabled`` for
                this one call (e.g. skip the shot on an unchanged DOM turn).
            wait_stable: wait for stability before extracting (default True).

        Never raises for a normal page — on failure it returns a partial
        ``BrowserState`` with ``error`` populated.
        """
        state = BrowserState(captured_at=time.time())
        want_shot = self.screenshot_cfg.enabled if screenshot is None else screenshot

        # 1. Stability (best-effort — a flaky wait must not block perception).
        if wait_stable:
            try:
                stable, reason = await self.wait_until_stable(page, frame)
                state.is_stable, state.stability_reason = stable, reason
            except Exception as exc:  # pragma: no cover - defensive
                state.is_stable, state.stability_reason = False, f"stability_error:{exc}"
        else:
            state.stability_reason = "skipped"

        # 2. Resolve the extraction context (frame or page).
        ctx: Any = page
        if frame is not None:
            live = await self._resolve_frame(frame)
            if live is None:
                state.error = "frame_detached"
                state.from_frame = True
                # still try a page-level screenshot so the caller has something
                if want_shot:
                    state.screenshot = await self.capture_screenshot(page)
                return state
            ctx = live
            state.from_frame = True
            try:
                state.frame_url = live.url
            except Exception:
                pass

        # 3. One DOM sweep.
        raw = None
        try:
            raw = await ctx.evaluate(
                EXTRACT_STATE_JS,
                {
                    "maxFields": self.caps.max_fields,
                    "maxButtons": self.caps.max_buttons,
                    "maxMessages": self.caps.max_messages,
                    "maxModals": self.caps.max_modals,
                    "htmlMaxLen": self.caps.html_max_len,
                    "textMaxLen": self.caps.text_max_len,
                },
            )
        except Exception as exc:
            state.error = f"extract_failed:{exc}"
            logger.warning(f"[Perception] extraction failed: {exc}")

        if isinstance(raw, dict):
            self._populate_from_raw(state, raw)

        # 4. Screenshot (always page-level).
        if want_shot:
            state.screenshot = await self.capture_screenshot(page)

        # 5. Meta fallbacks if the JS nav block was unavailable.
        if not state.url:
            try:
                state.url = page.url
            except Exception:
                pass
        if not state.title:
            try:
                state.title = await page.title()
            except Exception:
                pass

        logger.debug(f"[Perception] {state.summary()}")
        return state

    async def wait_until_stable(
        self, page: Page, frame: Optional[Any] = None
    ) -> Tuple[bool, str]:
        """Wait for the page/frame to settle. Returns ``(is_stable, reason)``.

        Two-stage: first (best-effort) reach network-idle, then run a
        MutationObserver quiescence probe that resolves once the DOM has been
        quiet for ``quiet_ms`` AND ``readyState === 'complete'``. Neither stage
        raising is fatal — a page that never idles still returns
        ``(False, 'timeout')`` and the caller can perceive it anyway.
        """
        cfg = self.stability_cfg

        if cfg.wait_network_idle:
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=cfg.network_idle_timeout_ms
                )
            except Exception:
                pass  # SPAs with long-poll / websockets never idle — that's fine

        ctx: Any = page
        if frame is not None:
            live = await self._resolve_frame(frame)
            if live is None:
                return False, "frame_detached"
            ctx = live
            try:
                await ctx.wait_for_load_state("domcontentloaded", timeout=3_000)
            except Exception:
                pass

        try:
            result = await ctx.evaluate(
                WAIT_STABLE_JS,
                {"quietMs": cfg.quiet_ms, "timeoutMs": cfg.timeout_ms},
            )
            if isinstance(result, dict):
                return bool(result.get("stable")), str(result.get("reason") or "unknown")
            return False, "no_result"
        except Exception as exc:
            return False, f"probe_error:{exc}"

    async def capture_screenshot(self, page: Page) -> Optional[bytes]:
        """Screenshot pipeline. Returns image bytes, or None on failure.

        Never raises — a screenshot failure must not abort perception.
        """
        cfg = self.screenshot_cfg
        try:
            kwargs: dict = {
                "full_page": cfg.full_page,
                "type": cfg.fmt,
                "timeout": cfg.timeout_ms,
            }
            if cfg.fmt == "jpeg":
                kwargs["quality"] = cfg.quality
            return await page.screenshot(**kwargs)
        except Exception as exc:
            logger.warning(f"[Perception] screenshot failed: {exc}")
            # Retry once, viewport-only — full_page can time out on very tall SPAs.
            try:
                kwargs["full_page"] = False
                return await page.screenshot(**kwargs)
            except Exception as exc2:
                logger.warning(f"[Perception] viewport screenshot also failed: {exc2}")
                return None

    # ─────────────────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────────────────
    async def _resolve_frame(self, frame: Any) -> Optional[Frame]:
        """Resolve a Frame or FrameLocator into a live, attached Frame."""
        try:
            if isinstance(frame, Frame):
                return None if frame.is_detached() else frame
            return await get_live_frame(frame)
        except Exception as exc:
            logger.debug(f"[Perception] frame resolve failed: {exc}")
            return None

    @staticmethod
    def _populate_from_raw(state: BrowserState, raw: dict) -> None:
        nav = raw.get("nav") or {}
        state.navigation = NavigationState(
            url=str(nav.get("url") or ""),
            title=str(nav.get("title") or ""),
            ready_state=str(nav.get("readyState") or ""),
            is_loading=bool(nav.get("isLoading")),
            frame_count=int(nav.get("frameCount") or 0),
            referrer=str(nav.get("referrer") or ""),
            visibility_state=str(nav.get("visibilityState") or ""),
        )
        state.url = state.navigation.url or state.url
        state.title = state.navigation.title or state.title
        state.html = str(raw.get("html") or "")
        state.visible_text = str(raw.get("visibleText") or "")

        state.forms = [
            FormElement(
                selector=str(f.get("selector") or ""),
                action=f.get("action"),
                method=f.get("method"),
                field_count=int(f.get("fieldCount") or 0),
                field_selectors=[str(s) for s in (f.get("fieldSelectors") or [])],
                visible=bool(f.get("visible", True)),
            )
            for f in (raw.get("forms") or [])
        ]

        state.inputs = [
            InputField(
                selector=str(i.get("selector") or ""),
                field_type=str(i.get("fieldType") or "text"),
                label=str(i.get("label") or ""),
                name=i.get("name"),
                element_id=i.get("elementId"),
                value=str(i.get("value") or ""),
                placeholder=i.get("placeholder"),
                required=bool(i.get("required")),
                disabled=bool(i.get("disabled")),
                readonly=bool(i.get("readonly")),
                checked=i.get("checked"),
                options=([str(o) for o in i["options"]] if i.get("options") else None),
                visible=bool(i.get("visible", True)),
            )
            for i in (raw.get("inputs") or [])
        ]

        state.buttons = [
            ButtonElement(
                selector=str(b.get("selector") or ""),
                text=str(b.get("text") or ""),
                button_type=b.get("buttonType"),
                disabled=bool(b.get("disabled")),
                visible=bool(b.get("visible", True)),
                is_submit=bool(b.get("isSubmit")),
            )
            for b in (raw.get("buttons") or [])
        ]

        state.messages = [
            PageMessage(
                kind=str(m.get("kind") or "alert"),
                text=str(m.get("text") or ""),
                selector=m.get("selector"),
                associated_field=m.get("associatedField"),
            )
            for m in (raw.get("messages") or [])
        ]

        state.modals = [
            ModalElement(
                selector=str(m.get("selector") or ""),
                text_preview=str(m.get("textPreview") or ""),
                role=m.get("role"),
                has_close_button=bool(m.get("hasCloseButton")),
                close_selector=m.get("closeSelector"),
                looks_like_application=bool(m.get("looksLikeApplication")),
            )
            for m in (raw.get("modals") or [])
        ]

        cap = raw.get("captcha") or {}
        state.captcha_present = bool(cap.get("present"))
        state.captcha_kind = str(cap.get("kind") or "")


# ─────────────────────────────────────────────────────────────────────────────
# Screenshot pipeline helpers (in-memory bytes → disk, for debugging/tests)
# ─────────────────────────────────────────────────────────────────────────────

def save_screenshot(state_or_bytes: Any, path: str) -> Optional[str]:
    """Persist screenshot bytes to ``path``. Accepts a BrowserState or raw bytes.

    Returns the path on success, else None. This is a debugging/local helper —
    Supabase upload of *final* screenshots stays in
    ``services/screenshot.py`` and is intentionally not duplicated here.
    """
    data: Optional[bytes]
    if isinstance(state_or_bytes, BrowserState):
        data = state_or_bytes.screenshot
    elif isinstance(state_or_bytes, (bytes, bytearray)):
        data = bytes(state_or_bytes)
    else:
        data = None
    if not data:
        return None
    try:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path
    except Exception as exc:
        logger.warning(f"[Perception] save_screenshot failed: {exc}")
        return None
