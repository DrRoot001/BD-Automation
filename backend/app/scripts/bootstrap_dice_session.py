"""One-time Dice login → persist storage_state for reuse.

Dice bot-throttles repeated logins. To avoid re-logging-in every run, we log
in ONCE here and save the full storage_state (cookies + localStorage) to
backend/data/sessions/dice.json. The BrowserContextManager auto-loads that
file for platform='dice' on every subsequent run, so the automation never
hits the login form again until the session expires.

Re-run this only when the saved session has expired (the live run will report
a login wall again).

Usage (from backend/):
    python -m app.scripts.bootstrap_dice_session
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv(dotenv_path=str(BACKEND_DIR / ".env"))

LOGIN = "https://www.dice.com/dashboard/login"
HOME_HINTS = ("home-feed", "dashboard")
OUT = BACKEND_DIR / "data" / "sessions" / "dice.json"

EMAIL = os.getenv("DICE_EMAIL", "").strip()
PW = os.getenv("DICE_PASSWORD", "").strip()


async def _wait_email(page, total_s: float = 90.0) -> bool:
    """Wait for the email field, reloading if the 'Checking your session' loader
    stalls (Dice throttles repeated checks — patience + reload gets past it)."""
    waited = 0.0
    while waited < total_s:
        if await page.locator("input[type='email']").count() > 0:
            try:
                if await page.locator("input[type='email']").first.is_visible():
                    return True
            except Exception:
                pass
        if "/login" not in page.url.lower():
            # Redirected away → already authenticated.
            return False
        await asyncio.sleep(3)
        waited += 3
        # Every 20s, reload to nudge a stalled session-check.
        if int(waited) % 20 == 0:
            print(f"  [t+{int(waited)}s] still waiting for email form; reloading…")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
    return False


async def main() -> int:
    if not EMAIL or not PW:
        print("ERROR: DICE_EMAIL / DICE_PASSWORD not set in backend/.env")
        return 2

    OUT.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=False,
                args=["--disable-blink-features=AutomationControlled", "--start-maximized"])
        except Exception:
            browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(viewport={"width": 1536, "height": 864})
        page = await ctx.new_page()

        print(f"Navigating to {LOGIN}")
        await page.goto(LOGIN, wait_until="domcontentloaded", timeout=40000)

        email_form = await _wait_email(page, total_s=120.0)
        if not email_form and any(h in page.url.lower() for h in HOME_HINTS):
            print(f"Already authenticated (url={page.url}). Saving session.")
        else:
            if not email_form:
                print("ERROR: email form never appeared (Dice may be hard-throttling). "
                      "Wait a few minutes and re-run.")
                await browser.close()
                return 1
            print(f"Logging in as {EMAIL}")
            await page.locator("input[type='email']").first.fill(EMAIL)
            await page.get_by_role("button", name="Continue with email").click()
            await page.locator("input[type='password']").first.wait_for(state="visible", timeout=20000)
            await page.locator("input[type='password']").first.fill(PW)
            await page.get_by_role("button", name="Sign In").click()

            # Wait until we leave the login flow.
            ok = False
            for _ in range(30):
                await asyncio.sleep(1)
                if "/login" not in page.url.lower():
                    ok = True
                    break
            if not ok:
                print(f"ERROR: login did not complete (url={page.url}).")
                await page.screenshot(path=str(BACKEND_DIR / "dice_bootstrap_fail.png"))
                await browser.close()
                return 1
            print(f"Login OK (url={page.url})")

        await asyncio.sleep(2)
        await ctx.storage_state(path=str(OUT))
        print(f"\n[OK] Saved Dice session -> {OUT}")
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
