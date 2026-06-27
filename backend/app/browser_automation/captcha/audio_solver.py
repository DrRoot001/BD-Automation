"""reCAPTCHA v2 audio-challenge solver — Whisper-backed.

Technique adapted from the public `capsolver` repo
(https://github.com/ibedevesh/capsolver, MIT licensed).

Differences from the upstream repo:
  * Async Playwright (our M4 runs `async_playwright`); the upstream uses
    `sync_playwright` which cannot coexist with an open async session.
  * Operates on an EXISTING `page` already mid-form-fill. The upstream
    launches its own browser, which would lose our session cookies and
    produce a token bound to the wrong session — useless for our case.
  * Lazy Whisper model load — first call pays the model-download cost,
    subsequent calls reuse the in-process model.

Solver flow (matches the upstream technique step-for-step):
  1. Locate the main reCAPTCHA anchor iframe and click the checkbox.
  2. If the challenge appears, locate the bframe and switch to audio mode.
  3. Read the audio-challenge `.mp3` URL from `.rc-audiochallenge-tdownload-link`.
  4. Download the MP3 and transcribe with faster-whisper.
  5. Fill `#audio-response` and click `#recaptcha-verify-button`.
  6. Wait for `.recaptcha-checkbox-checked` and read the token out of
     `document.getElementById('g-recaptcha-response').value`.

Failure modes (all return None / False):
  * No reCAPTCHA iframe on page → not a captcha wall, return None.
  * Whisper not installed → return None (caller falls back to existing AI / paid layer).
  * Google flags the session ("Try again later") → return None.
  * Audio download fails → return None.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from playwright.async_api import Page, Frame

logger = logging.getLogger(__name__)


# Whisper model size — env-configurable. Capsolver README:
#   tiny (fastest) < base (recommended) < small < medium < large-v3 (best).
# `base` ≈ 75 MB, downloaded on first use and cached under HOME.
_WHISPER_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")
_WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")  # CPU-friendly default

# Module-level model cache — lazily initialised in `_get_model`.
_model = None
_model_lock = asyncio.Lock()


@dataclass
class AudioSolveResult:
    success: bool
    token: Optional[str] = None
    error: Optional[str] = None


async def _get_model():
    """Lazy-import and cache the faster-whisper model. Heavy import → only
    pay the cost when a captcha actually appears."""
    global _model
    if _model is not None:
        return _model
    async with _model_lock:
        if _model is not None:
            return _model
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise RuntimeError(
                f"faster-whisper not installed: {exc}. "
                "Install via `pip install faster-whisper>=1.0.0`."
            ) from exc
        logger.info(
            f"[audio-captcha] loading Whisper model size={_WHISPER_SIZE} "
            f"compute={_WHISPER_COMPUTE} (first-call cost)"
        )
        loop = asyncio.get_event_loop()
        _model = await loop.run_in_executor(
            None,
            lambda: WhisperModel(_WHISPER_SIZE, device="cpu", compute_type=_WHISPER_COMPUTE),
        )
        return _model


async def _transcribe(audio_path: str) -> str:
    model = await _get_model()
    loop = asyncio.get_event_loop()
    def _do():
        # `beam_size=5` matches the upstream repo's setting.
        segments, _ = model.transcribe(audio_path, beam_size=5)
        return " ".join(s.text for s in segments).strip()
    text = await loop.run_in_executor(None, _do)
    # Google's audio challenge speaks digits or short words.
    # Strip non-alphanumeric (the upstream solver does the same).
    cleaned = "".join(c for c in text.lower() if c.isalnum() or c.isspace()).strip()
    return cleaned


async def _download_audio(url: str, dest_dir: Optional[str] = None) -> Optional[str]:
    """Download the reCAPTCHA audio MP3 to a temp file. Returns the path."""
    if not url:
        return None
    dest = Path(dest_dir or tempfile.gettempdir()) / "recaptcha_audio.mp3"
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            r = await client.get(url)
        if r.status_code != 200 or not r.content:
            logger.warning(f"[audio-captcha] audio download HTTP {r.status_code}")
            return None
        dest.write_bytes(r.content)
        return str(dest)
    except Exception as exc:
        logger.warning(f"[audio-captcha] audio download failed: {exc}")
        return None


async def solve_recaptcha_v2_via_audio(
    page: Page,
    frame: Optional[Frame] = None,
    max_retries: int = 2,
) -> AudioSolveResult:
    """Solve any reCAPTCHA v2 visible on the current page using Whisper.

    Returns an `AudioSolveResult`. On success the token is also already
    committed to `document.getElementById('g-recaptcha-response').value`
    by Google's verify handler — most forms don't need the token passed
    explicitly, just need the checkbox checked.
    """
    ctx = frame or page

    # ─── Step 1: locate the anchor iframe ───────────────────────────────
    anchor = None
    for fr in page.frames:
        try:
            src = fr.url or ""
        except Exception:
            continue
        if "recaptcha" in src and "anchor" in src:
            anchor = fr
            break
    if anchor is None:
        # Try a softer pattern — older anchor iframes don't have "anchor"
        # in the URL, just "recaptcha".
        for fr in page.frames:
            src = (fr.url or "")
            if "recaptcha" in src and "bframe" not in src:
                anchor = fr
                break
    if anchor is None:
        return AudioSolveResult(success=False, error="no_recaptcha_iframe")

    # ─── Step 2: click the checkbox ─────────────────────────────────────
    # The anchor iframe can be present in page.frames before its DOM has
    # painted the #recaptcha-anchor checkbox, so a bare click times out.
    # Wait for it, scroll it into view, and fall back to a forced click /
    # JS click before giving up. Retry the whole sequence twice.
    cb = anchor.locator("#recaptcha-anchor")
    clicked = False
    last_cb_err: Optional[str] = None
    for cb_attempt in range(1, 4):
        try:
            await cb.wait_for(state="visible", timeout=8000)
            try:
                await cb.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            await cb.click(timeout=4000)
            clicked = True
            break
        except Exception as exc:
            last_cb_err = str(exc)
            # Fallback: force-click, then a raw JS click on the checkbox.
            try:
                await cb.click(timeout=2500, force=True)
                clicked = True
                break
            except Exception:
                try:
                    await anchor.evaluate(
                        "() => { const el = document.querySelector('#recaptcha-anchor'); if (el) el.click(); }"
                    )
                    clicked = True
                    break
                except Exception as exc2:
                    last_cb_err = f"{exc} | js: {exc2}"
            await asyncio.sleep(1.5)
    if not clicked:
        return AudioSolveResult(success=False, error=f"checkbox_click_failed: {last_cb_err}")
    await asyncio.sleep(2.0)

    # If it passed without a challenge, we're done.
    try:
        if await anchor.locator(".recaptcha-checkbox-checked").count() > 0:
            token = await _read_token(page)
            return AudioSolveResult(success=True, token=token)
    except Exception:
        pass

    # ─── Step 3: switch to audio mode in the challenge iframe ───────────
    bframe = None
    for fr in page.frames:
        if "bframe" in (fr.url or ""):
            bframe = fr
            break
    if bframe is None:
        return AudioSolveResult(success=False, error="no_challenge_iframe")

    last_err: Optional[str] = None
    for attempt in range(1, max_retries + 1):
        try:
            await bframe.locator("#recaptcha-audio-button").click(timeout=4000)
        except Exception as exc:
            # Already in audio mode? Continue.
            logger.debug(f"[audio-captcha] audio-button click skipped: {exc}")
        await asyncio.sleep(2.0)

        # Detect Google's hard block ("Try again later — your computer or
        # network may be sending automated queries"). ONLY the
        # `.rc-doscaptcha-header-text` element signals a real block, and only
        # when it's VISIBLE with non-empty text.
        #
        # IMPORTANT: `.rc-audiochallenge-error-message` is NOT a block signal —
        # it's the always-present (empty, hidden) container where per-attempt
        # transcription errors would render. Treating its mere presence as a
        # block was a false positive that bailed out with `google_blocked: ''`
        # before we ever downloaded the audio.
        try:
            dos = bframe.locator(".rc-doscaptcha-header-text").first
            if await dos.count() > 0 and await dos.is_visible():
                txt = (await dos.inner_text(timeout=1500)).strip()
                if txt:
                    logger.warning(
                        f"[audio-captcha] Google hard-blocked audio challenge: {txt!r}"
                    )
                    return AudioSolveResult(
                        success=False, error=f"google_blocked: {txt[:80]}"
                    )
        except Exception:
            pass

        # ─── Step 4: read the audio URL ─────────────────────────────────
        try:
            audio_url = await bframe.locator(
                ".rc-audiochallenge-tdownload-link"
            ).get_attribute("href", timeout=5000)
        except Exception as exc:
            last_err = f"audio_url_missing: {exc}"
            continue
        if not audio_url:
            last_err = "audio_url_empty"
            continue

        # ─── Step 5: download + transcribe ──────────────────────────────
        audio_path = await _download_audio(audio_url)
        if not audio_path:
            last_err = "audio_download_failed"
            continue
        try:
            transcript = await _transcribe(audio_path)
        except Exception as exc:
            last_err = f"transcription_failed: {exc}"
            continue
        if not transcript:
            last_err = "empty_transcript"
            continue
        logger.info(f"[audio-captcha] transcript={transcript!r} (attempt {attempt})")

        # ─── Step 6: submit and verify ──────────────────────────────────
        try:
            await bframe.locator("#audio-response").fill(transcript, timeout=4000)
            await bframe.locator("#recaptcha-verify-button").click(timeout=4000)
        except Exception as exc:
            last_err = f"submit_failed: {exc}"
            continue
        await asyncio.sleep(2.5)

        # Did the anchor checkbox flip to checked?
        try:
            if await anchor.locator(".recaptcha-checkbox-checked").count() > 0:
                token = await _read_token(page)
                logger.info(
                    f"[audio-captcha] solved on attempt {attempt} "
                    f"(token_len={len(token or '')})"
                )
                return AudioSolveResult(success=True, token=token)
        except Exception:
            pass

        # Check for a real per-attempt rejection — the error element must be
        # VISIBLE with actual text (e.g. "Multiple correct solutions
        # required"). An empty/hidden error container is not a rejection.
        try:
            err = bframe.locator(".rc-audiochallenge-error-message").first
            if await err.count() > 0 and await err.is_visible():
                etxt = (await err.inner_text(timeout=1000)).strip()
                if etxt:
                    last_err = f"google_rejected_transcript_attempt_{attempt}: {etxt[:50]}"
                    continue
        except Exception:
            pass

        last_err = f"verify_did_not_check_anchor_attempt_{attempt}"

    return AudioSolveResult(success=False, error=last_err or "max_retries_exhausted")


async def _read_token(page: Page) -> Optional[str]:
    """Read the g-recaptcha-response token out of the main frame. Returns None
    if absent — many forms don't need it client-side anyway because the
    verify handler stamps it server-side via the iframe."""
    try:
        return await page.evaluate(
            "() => (document.getElementById('g-recaptcha-response')||{}).value || null"
        )
    except Exception:
        return None


async def detect_recaptcha_v2(page: Page) -> bool:
    """Quick predicate: is there a reCAPTCHA v2 widget visible on the page?

    Caller (CaptchaService) uses this to decide whether to even instantiate
    the Whisper model — keeps the cold path cheap on captcha-free forms.
    """
    try:
        # Either the `.g-recaptcha` div container OR an anchor iframe.
        n = await page.evaluate(
            "() => document.querySelectorAll("
            "  '.g-recaptcha, iframe[src*=\"recaptcha\"]'"
            ").length"
        )
        return bool(n)
    except Exception:
        return False
