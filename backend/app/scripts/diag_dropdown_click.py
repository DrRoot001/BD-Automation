"""Diagnose why custom-dropdown click+select isn't committing values."""
from __future__ import annotations
import asyncio, sys
from pathlib import Path
BACKEND = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND))
from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

URL = "https://job-boards.greenhouse.io/scaleai/jobs/4618065005"

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)

        print("=== STEP 1: try clicking #country (the inner input) ===")
        await page.locator("#country").click(force=True)
        await page.wait_for_timeout(800)
        opened_a = await page.evaluate(
            "() => !!document.querySelector('.select__menu, .select__menu-list, [class*=menu]')"
        )
        print(f"  menu open after clicking #country: {opened_a}")
        # Sample what's actually visible
        sample = await page.evaluate("""() => {
            const m = document.querySelector('.select__menu, [class*="menu"]');
            if (!m) return null;
            return {
                cls: m.className.slice(0,80),
                rect: m.getBoundingClientRect(),
                options: Array.from(m.querySelectorAll('[role=option], .select__option, [class*=option]'))
                    .slice(0,5).map(o => o.textContent.slice(0,40))
            };
        }""")
        print(f"  menu sample: {sample}")

        # Close
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)

        print()
        print("=== STEP 2: try clicking the .select__control wrapper instead ===")
        # Walk up from #country to .select__control
        wrapper_info = await page.evaluate("""() => {
            const el = document.getElementById('country');
            if (!el) return null;
            const ctrl = el.closest('.select__control');
            if (!ctrl) return null;
            ctrl.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
            ctrl.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
            return { cls: ctrl.className, rect: ctrl.getBoundingClientRect() };
        }""")
        print(f"  wrapper: {wrapper_info}")
        await page.wait_for_timeout(800)
        opened_b = await page.evaluate(
            "() => !!document.querySelector('.select__menu, [class*=menu]')"
        )
        print(f"  menu open after mousedown on wrapper: {opened_b}")

        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)

        print()
        print("=== STEP 3: click the .select__control with playwright locator ===")
        await page.locator(".select__control").first.click(force=True)
        await page.wait_for_timeout(800)
        opened_c = await page.evaluate(
            "() => !!document.querySelector('.select__menu, [class*=menu]')"
        )
        sample_c = await page.evaluate("""() => {
            const m = document.querySelector('.select__menu, [class*="menu"]');
            if (!m) return null;
            return Array.from(m.querySelectorAll('[role=option], .select__option, [class*=option]'))
                .slice(0,10).map(o => o.textContent.trim().slice(0,60));
        }""")
        print(f"  menu open: {opened_c}")
        print(f"  options visible: {sample_c}")

        print()
        print("=== STEP 4: file input visual state ===")
        finfo = await page.evaluate("""() => {
            const r = document.getElementById('resume');
            return r ? {
                disabled: r.disabled,
                readonly: r.readOnly,
                cssDisplay: window.getComputedStyle(r).display,
                cssVisibility: window.getComputedStyle(r).visibility,
                cssOpacity: window.getComputedStyle(r).opacity,
                cssPointerEvents: window.getComputedStyle(r).pointerEvents,
                rect: r.getBoundingClientRect(),
                parentClass: r.parentElement ? r.parentElement.className.slice(0,80) : '',
            } : null;
        }""")
        print(f"  #resume: {finfo}")

        await browser.close()

asyncio.run(main())
