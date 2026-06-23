#!/usr/bin/env python3
"""Standalone reCAPTCHA v2 solver test — NO LLM, NO DB, NO form flow.

Loads a page that actually shows reCAPTCHA v2 and calls our Whisper-based
audio solver directly (the capsolver technique, vendored from
https://github.com/ibedevesh/capsolver). This is the clean way to verify
the captcha solver works end-to-end without the whole M4 form pipeline or
any LLM quota.

Usage:
    python test_capsolver.py                  # Google's reCAPTCHA v2 demo
    python test_capsolver.py "<url>"          # any page with reCAPTCHA v2
    set PLAYWRIGHT_HEADLESS=true               # run headless (default false)

What it does:
    1. Launch Chromium (visible by default so you can watch the audio solve).
    2. Navigate to the target page, wait for the reCAPTCHA iframe.
    3. Call solve_recaptcha_v2_via_audio(page).
    4. Print SUCCESS + token length, or the precise failure reason.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

# Windows consoles default to cp1252 and crash on any non-ASCII output.
# Force UTF-8 so log lines never raise UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DEFAULT_URL = "https://www.google.com/recaptcha/api2/demo"


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    headless = os.getenv("PLAYWRIGHT_HEADLESS", "false").lower() == "true"

    from playwright.async_api import async_playwright
    from app.browser_automation.captcha.audio_solver import (
        solve_recaptcha_v2_via_audio,
        detect_recaptcha_v2,
    )

    print(f"[capsolver-test] url={url}  headless={headless}")
    print(f"[capsolver-test] WHISPER_MODEL_SIZE={os.getenv('WHISPER_MODEL_SIZE', 'base')}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await asyncio.sleep(2.5)

            has_captcha = await detect_recaptcha_v2(page)
            print(f"[capsolver-test] reCAPTCHA v2 detected on page: {has_captcha}")
            if not has_captcha:
                print("[capsolver-test] No reCAPTCHA widget found — nothing to solve.")
                return 2

            print("[capsolver-test] Solving via Whisper audio challenge "
                  "(first run downloads the model — be patient)…")
            result = await solve_recaptcha_v2_via_audio(page, max_retries=3)

            if result.success:
                tok = result.token or ""
                print(f"[capsolver-test] SUCCESS - token_len={len(tok)} "
                      f"token_head={tok[:24]!r}")
                # Capture proof
                shot = PROJECT_ROOT / "test_capsolver_evidence.png"
                try:
                    await page.screenshot(path=str(shot))
                    print(f"[capsolver-test] screenshot → {shot}")
                except Exception:
                    pass
                return 0
            else:
                print(f"[capsolver-test] FAILED - reason={result.error!r}")
                if result.error and "google_blocked" in str(result.error):
                    print("[capsolver-test] NOTE: 'Try again later' = Google blocked the "
                          "audio challenge for this session (common on headless / "
                          "datacenter IP / flagged fingerprint). Retry on a real "
                          "headed browser + residential IP, or after a cooldown.")
                return 1
        finally:
            if not headless:
                await asyncio.sleep(3.0)  # let you see the result
            await context.close()
            await browser.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
