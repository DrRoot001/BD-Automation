"""AI-vision captcha solver.

This module gives Module 4 a built-in, zero-extra-cost captcha solver that
uses the same Claude vision model the AgentLoop already speaks to. It is
deliberately NOT a replacement for paid services like 2Captcha or
AntiCaptcha — for high-volume production use those remain more reliable
on Google reCAPTCHA v2 because they farm challenges to humans. But for
the common cases this codebase actually hits, the AI can:

  1. **reCAPTCHA v2 checkbox-pass** — click the "I'm not a robot" checkbox
     and observe whether Google passed it silently based on behavioral
     signals (stealth + slow human-like delays often work). No challenge
     image, no OCR required.

  2. **Text/OCR captchas** — read the distorted text directly from the
     captcha image with Claude vision and return the answer. This is
     what Ocilar does, but free.

  3. **Image-grid challenges** — for "select all squares with a bus"
     style reCAPTCHA v2 image challenges, ask Claude to identify the
     matching tiles and return their indices. We click those tiles in
     turn. Honest limitation: Google detects automation here and often
     re-challenges, but a fraction passes.

  4. **hCaptcha image-grid** — same approach as #3 against hCaptcha.

The class shares the same ClaudeClient singleton the rest of M4 uses, so
its calls show up in the token telemetry under label "captcha_solver.*".
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import List, Optional

from playwright.async_api import Page

from ..llm import LLMUnavailable, get_llm
from ..llm import telemetry as _tele
from .models import CaptchaSolution

logger = logging.getLogger(__name__)


# Anchor frame = the small box with the "I'm not a robot" checkbox.
# Bframe = the larger image-challenge dialog that may appear after.
_RECAPTCHA_ANCHOR = (
    "iframe[src*='recaptcha/api2/anchor'], "
    "iframe[src*='recaptcha/enterprise/anchor']"
)
_RECAPTCHA_BFRAME = (
    "iframe[src*='recaptcha/api2/bframe'], "
    "iframe[src*='recaptcha/enterprise/bframe']"
)
_HCAPTCHA_ANCHOR = "iframe[src*='hcaptcha.com/captcha']"


_OCR_PROMPT = (
    "You are reading a captcha image. The image contains distorted text "
    "(letters and digits). Return STRICT JSON: {\"text\": \"<answer>\"}. "
    "Use only the characters visible in the image. No prose, no explanation."
)


_GRID_PROMPT_TEMPLATE = (
    "You are looking at a captcha image-selection grid. The user is asked: "
    "\"{instruction}\". The image is split into a {rows}x{cols} grid, "
    "numbered LEFT-TO-RIGHT then TOP-TO-BOTTOM starting from 0. "
    "Return STRICT JSON: {{\"tiles\": [<list of integer indices of tiles that match the instruction>]}}. "
    "Be precise — only include tiles where the requested object is clearly visible. "
    "No prose, no extra fields."
)


@dataclass
class AIChallenge:
    """A captured captcha challenge ready for AI solving."""
    kind: str   # "text_ocr" | "grid_select" | "checkbox_pass" | "unknown"
    image_b64: Optional[str] = None
    instruction: Optional[str] = None
    rows: int = 3
    cols: int = 3


class AICaptchaSolver:
    """Best-effort AI-driven captcha solver.

    Public surface mirrors ``CaptchaService.solve()`` so callers can drop
    it in as ``provider="ai"``.
    """

    def __init__(self):
        self._llm = get_llm()

    # ──────────────────────────────────────────────────────────────────────
    # Step 1 — checkbox pass attempt
    # ──────────────────────────────────────────────────────────────────────

    async def attempt_checkbox_pass(self, page: Page) -> bool:
        """Click the 'I'm not a robot' checkbox with realistic human pacing
        and return True if Google passed it without escalating to a challenge.
        """
        try:
            frame_el = await page.query_selector(_RECAPTCHA_ANCHOR)
            if not frame_el:
                return False
            anchor = await frame_el.content_frame()
            if not anchor:
                return False
            checkbox = anchor.locator("#recaptcha-anchor")
            if await checkbox.count() == 0:
                return False
            # Pre-click: random small mouse jiggle to plant a behavioral signal
            box = await frame_el.bounding_box()
            if box:
                try:
                    await page.mouse.move(box["x"] + 30, box["y"] + 30, steps=12)
                    await asyncio.sleep(0.4)
                    await page.mouse.move(box["x"] + 50, box["y"] + 25, steps=8)
                    await asyncio.sleep(0.25)
                except Exception:
                    pass
            await checkbox.click(timeout=8000)
            # Wait for Google to either: (a) check the box silently, or
            # (b) open the bframe challenge. Up to 4 seconds.
            for _ in range(20):
                await asyncio.sleep(0.2)
                aria_checked = await checkbox.get_attribute("aria-checked")
                if aria_checked == "true":
                    logger.info("[AICaptcha] checkbox passed silently — no challenge")
                    return True
                bframe = await page.query_selector(_RECAPTCHA_BFRAME)
                if bframe:
                    bf = await bframe.content_frame()
                    if bf:
                        try:
                            visible = await bf.locator(".rc-imageselect-payload, "
                                                       ".rc-audiochallenge-instructions").first.is_visible(timeout=400)
                            if visible:
                                logger.info("[AICaptcha] checkbox triggered image challenge")
                                return False
                        except Exception:
                            continue
            return False
        except Exception as exc:
            logger.warning(f"[AICaptcha] checkbox attempt failed: {exc}")
            return False

    async def attempt_hcaptcha_checkbox_pass(self, page: Page) -> Optional[str]:
        """Click the hCaptcha checkbox and, if it passes WITHOUT escalating to a
        visible grid, return the `h-captcha-response` token.

        AI-vision GRID solving for hCaptcha is intentionally NOT implemented in
        this class — hCaptcha's grid differs from reCAPTCHA's and the project's
        real hCaptcha solver is `hcaptcha-challenger` (AgentV) in agent/loop.py
        (used for Lever, whose hCaptcha is invisible anyway). This method only
        covers the common "checkbox passes silently" case; it returns None when
        a challenge opens so the caller can fall back to AgentV rather than
        silently no-op'ing on reCAPTCHA-only selectors (the prior behavior)."""
        try:
            frame_el = await page.query_selector(_HCAPTCHA_ANCHOR)
            if not frame_el:
                return None
            anchor = await frame_el.content_frame()
            if not anchor:
                return None
            checkbox = anchor.locator("#checkbox")
            if await checkbox.count() == 0:
                return None
            box = await frame_el.bounding_box()
            if box:
                try:
                    await page.mouse.move(box["x"] + 15, box["y"] + 15, steps=10)
                    await asyncio.sleep(0.3)
                except Exception:
                    pass
            await checkbox.click(timeout=8000)
            for _ in range(20):  # up to ~4s
                await asyncio.sleep(0.2)
                token = await page.evaluate(
                    "() => { const t = document.querySelector("
                    "'textarea[name=\"h-captcha-response\"], #h-captcha-response'); "
                    "return t && t.value ? t.value : ''; }"
                )
                if token:
                    logger.info("[AICaptcha] hCaptcha passed via checkbox — token captured")
                    return token
            logger.info(
                "[AICaptcha] hCaptcha checkbox did not yield a token (challenge "
                "likely opened) — AI-vision grid solving is not supported for "
                "hCaptcha; caller should fall back to hcaptcha-challenger (AgentV)."
            )
            return None
        except Exception as exc:
            logger.warning(f"[AICaptcha] hCaptcha checkbox attempt failed: {exc}")
            return None

    # ──────────────────────────────────────────────────────────────────────
    # Step 2 — capture the challenge into something the LLM can read
    # ──────────────────────────────────────────────────────────────────────

    async def _capture_text_captcha(self, page: Page) -> Optional[AIChallenge]:
        """Find a non-iframe text-style captcha image and base64-encode it."""
        # Common selectors used by Greenhouse / Lever / Workday text captchas
        selectors = [
            "img[src*='captcha']",
            "img[alt*='captcha' i]",
            ".captcha img",
            "img.captcha-image",
        ]
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    buf = await el.screenshot()
                    return AIChallenge(kind="text_ocr",
                                       image_b64=base64.b64encode(buf).decode())
            except Exception:
                continue
        return None

    async def _capture_recaptcha_grid(self, page: Page) -> Optional[AIChallenge]:
        """Screenshot the reCAPTCHA bframe image grid and extract the prompt."""
        try:
            bframe_el = await page.query_selector(_RECAPTCHA_BFRAME)
            if not bframe_el:
                return None
            bf = await bframe_el.content_frame()
            if not bf:
                return None
            # Extract the instruction (e.g. "Select all squares with traffic lights")
            try:
                instr_el = bf.locator(".rc-imageselect-desc-no-canonical, "
                                      ".rc-imageselect-desc-wrapper").first
                instruction = (await instr_el.text_content(timeout=2000) or "").strip()
            except Exception:
                instruction = "Select all matching squares"
            # Screenshot the image area
            img_area = bf.locator(".rc-imageselect-payload").first
            try:
                buf = await img_area.screenshot(timeout=4000)
            except Exception:
                # Fall back to full bframe screenshot
                buf = await bframe_el.screenshot(timeout=4000)
            # reCAPTCHA grids are 3x3 by default; the dynamic 4x4 variant
            # exists too but the instruction text usually tells us.
            rows = 4 if "4x4" in instruction.lower() else 3
            return AIChallenge(
                kind="grid_select",
                image_b64=base64.b64encode(buf).decode(),
                instruction=instruction,
                rows=rows,
                cols=rows,
            )
        except Exception as exc:
            logger.warning(f"[AICaptcha] reCAPTCHA grid capture failed: {exc}")
            return None

    # ──────────────────────────────────────────────────────────────────────
    # Step 3 — call Claude vision to solve
    # ──────────────────────────────────────────────────────────────────────

    async def _solve_text_ocr(self, challenge: AIChallenge) -> Optional[str]:
        if not challenge.image_b64:
            return None
        _tele.set_label("captcha_solver.text_ocr")
        try:
            data = await self._llm.generate_json(
                prompt=_OCR_PROMPT,
                image_bytes=base64.b64decode(challenge.image_b64),
                temperature=0.0,
                timeout_s=30,
            )
        except LLMUnavailable as exc:
            logger.warning(f"[AICaptcha] LLM OCR failed: {exc}")
            return None
        text = (data or {}).get("text") if isinstance(data, dict) else None
        if not text or not isinstance(text, str):
            return None
        # Strip everything that obviously can't be in a captcha
        text = re.sub(r"\s+", "", text).strip()
        return text or None

    async def _solve_grid(self, challenge: AIChallenge) -> List[int]:
        if not challenge.image_b64:
            return []
        prompt = _GRID_PROMPT_TEMPLATE.format(
            instruction=challenge.instruction or "Select all matching squares",
            rows=challenge.rows, cols=challenge.cols,
        )
        _tele.set_label("captcha_solver.grid")
        try:
            data = await self._llm.generate_json(
                prompt=prompt,
                image_bytes=base64.b64decode(challenge.image_b64),
                temperature=0.0,
                timeout_s=45,
            )
        except LLMUnavailable as exc:
            logger.warning(f"[AICaptcha] LLM grid failed: {exc}")
            return []
        tiles_raw = (data or {}).get("tiles") if isinstance(data, dict) else None
        if not isinstance(tiles_raw, list):
            return []
        out: List[int] = []
        max_tile = challenge.rows * challenge.cols - 1
        for t in tiles_raw:
            try:
                i = int(t)
            except (TypeError, ValueError):
                continue
            if 0 <= i <= max_tile:
                out.append(i)
        return sorted(set(out))

    # ──────────────────────────────────────────────────────────────────────
    # Step 4 — apply the solution (inject text / click tiles)
    # ──────────────────────────────────────────────────────────────────────

    async def _click_grid_tiles(self, page: Page, tiles: List[int],
                                rows: int, cols: int) -> bool:
        """Click the given tile indices inside the reCAPTCHA bframe."""
        if not tiles:
            return False
        try:
            bframe_el = await page.query_selector(_RECAPTCHA_BFRAME)
            if not bframe_el:
                return False
            bf = await bframe_el.content_frame()
            if not bf:
                return False
            tile_locs = bf.locator(".rc-imageselect-tile")
            count = await tile_locs.count()
            if count == 0:
                # Newer reCAPTCHA uses td.rc-image-tile-wrapper
                tile_locs = bf.locator("td.rc-image-tile-wrapper")
                count = await tile_locs.count()
            if count == 0:
                return False
            for idx in tiles:
                if idx >= count:
                    continue
                try:
                    await tile_locs.nth(idx).click(timeout=4000)
                    # Human-paced delay between tile clicks — defeats some
                    # naive automation detection
                    await asyncio.sleep(0.3 + (idx % 3) * 0.1)
                except Exception as exc:
                    logger.debug(f"[AICaptcha] tile {idx} click failed: {exc}")
            # Click Verify/Continue
            verify = bf.locator("#recaptcha-verify-button")
            if await verify.count() > 0:
                await verify.click(timeout=5000)
                await asyncio.sleep(2.0)
            return True
        except Exception as exc:
            logger.warning(f"[AICaptcha] grid click failed: {exc}")
            return False

    # ──────────────────────────────────────────────────────────────────────
    # Public — solve()
    # ──────────────────────────────────────────────────────────────────────

    async def solve(self, page: Page, captcha_type: str,
                    max_attempts: int = 2) -> CaptchaSolution:
        """Try to solve the captcha entirely in-process using Claude vision.

        Strategy by type:
          recaptcha_v2 — try checkbox pass first; if that opens a grid
            challenge, ask Claude to identify matching tiles and click.
          hcaptcha — checkbox-pass only (see attempt_hcaptcha_checkbox_pass);
            AI-vision grid solving is NOT supported here — caller should fall
            back to hcaptcha-challenger (AgentV) when this returns failure.
          image — capture the static captcha image and ask Claude to OCR it.
            Caller is responsible for typing the returned text into the
            answer field (we return token=<solved_text>).
        """
        start = time.monotonic()
        for attempt in range(1, max_attempts + 1):
            logger.info(f"[AICaptcha] attempt {attempt}/{max_attempts} type={captcha_type}")

            # hCaptcha: checkbox pass only. Do NOT run the reCAPTCHA grid
            # capture below (its selectors are reCAPTCHA-only and silently
            # no-op on hCaptcha — the old bug this replaces).
            if captcha_type == "hcaptcha":
                hc_token = await self.attempt_hcaptcha_checkbox_pass(page)
                if hc_token:
                    return CaptchaSolution(
                        captcha_type="hcaptcha", success=True, token=hc_token,
                        solve_time_seconds=time.monotonic() - start, cost_usd=0,
                    )
                # No token → fail fast (caller falls back to AgentV). Retrying
                # the checkbox rarely helps once a challenge has opened.
                break

            # Phase 1: try the silent checkbox pass for reCAPTCHA v2
            if captcha_type == "recaptcha_v2":
                passed = await self.attempt_checkbox_pass(page)
                if passed:
                    return CaptchaSolution(
                        captcha_type=captcha_type, success=True, token="checkbox_passed",
                        solve_time_seconds=time.monotonic() - start, cost_usd=0,
                    )
                # If checkbox didn't pass silently, fall through to grid
                challenge = await self._capture_recaptcha_grid(page)
                if challenge and challenge.image_b64:
                    tiles = await self._solve_grid(challenge)
                    logger.info(f"[AICaptcha] LLM chose tiles={tiles} "
                                f"for instruction={challenge.instruction!r}")
                    clicked = await self._click_grid_tiles(
                        page, tiles, challenge.rows, challenge.cols,
                    )
                    if clicked:
                        # Wait for the iframe to either re-challenge or close
                        await asyncio.sleep(3.0)
                        # Check if anchor checkbox now shows "checked"
                        frame_el = await page.query_selector(_RECAPTCHA_ANCHOR)
                        if frame_el:
                            anchor = await frame_el.content_frame()
                            if anchor:
                                cb = anchor.locator("#recaptcha-anchor")
                                if (await cb.get_attribute("aria-checked")) == "true":
                                    return CaptchaSolution(
                                        captcha_type=captcha_type, success=True,
                                        token="grid_solved",
                                        solve_time_seconds=time.monotonic() - start,
                                        cost_usd=0,
                                    )

            elif captcha_type == "image":
                challenge = await self._capture_text_captcha(page)
                if challenge and challenge.image_b64:
                    answer = await self._solve_text_ocr(challenge)
                    if answer:
                        logger.info(f"[AICaptcha] OCR → {answer!r}")
                        return CaptchaSolution(
                            captcha_type="image", success=True, token=answer,
                            solve_time_seconds=time.monotonic() - start, cost_usd=0,
                        )

            # If we got here, retry with widget refresh (handled by caller)
            if attempt < max_attempts:
                await asyncio.sleep(1.5)

        return CaptchaSolution(
            captcha_type=captcha_type, success=False,  # type: ignore[arg-type]
            solve_time_seconds=time.monotonic() - start, cost_usd=0,
        )
