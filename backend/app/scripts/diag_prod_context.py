"""Reproduce production context EXACTLY then try the dropdowns."""
from __future__ import annotations
import asyncio, sys
from pathlib import Path
BACKEND = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND))
from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

URL = "https://job-boards.greenhouse.io/scaleai/jobs/4618065005"


async def fill_dropdown(page, input_id: str, value: str):
    info = await page.evaluate("""(args) => {
        const el = document.getElementById(args.id);
        if (!el) return {step:'no-el'};
        const ctrl = el.closest('.select__control');
        if (!ctrl) return {step:'no-ctrl'};
        const cls_before = ctrl.className;
        ctrl.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:0}));
        ctrl.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, button:0}));
        return {step:'dispatched', cls_before, cls_after: ctrl.className};
    }""", {"id": input_id})
    print(f"  open #{input_id}: {info}")
    await page.wait_for_timeout(500)
    menu_open = await page.evaluate("""(id) => {
        const el = document.getElementById(id);
        const ctrl = el.closest('.select__control');
        return ctrl.className.includes('menu-is-open');
    }""", input_id)
    print(f"  menu-is-open: {menu_open}")


async def main():
    # Mimic prod context EXACTLY but without STEALTH_JS / playwright-stealth
    # to find which piece is breaking react-select.
    import os
    from playwright.async_api import async_playwright
    p = await async_playwright().start()
    # Test: viewport + UA + sec-ch-ua but UA Chrome version MATCHES sec-ch-ua
    browser = await p.chromium.launch(channel="chrome", headless=True)
    context = await browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        extra_http_headers={
            "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
    )
    print("viewport + UA(v136) + sec-ch-ua(v136) — matched versions")
    page = await context.new_page()

    await page.goto(URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    # Now try opening 3 dropdowns
    await fill_dropdown(page, "question_8384146005", "Yes")
    await fill_dropdown(page, "question_8384149005", "Yes")
    await fill_dropdown(page, "question_8384150005", "No")

    await context.close()
    print("done")


asyncio.run(main())
