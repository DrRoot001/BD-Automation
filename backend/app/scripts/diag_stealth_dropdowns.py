"""Does playwright-stealth break react-select?"""
from __future__ import annotations
import asyncio, sys
from pathlib import Path
BACKEND = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND))
from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

URL = "https://job-boards.greenhouse.io/scaleai/jobs/4618065005"


async def fill_dropdown(page, input_id: str, value: str):
    await page.evaluate("""(args) => {
        const el = document.getElementById(args.id);
        const ctrl = el.closest('.select__control');
        ctrl.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:0}));
        ctrl.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, button:0}));
    }""", {"id": input_id})
    await page.wait_for_timeout(500)
    await page.locator(f"#{input_id}").focus()
    await page.keyboard.type(value, delay=30)
    await page.wait_for_timeout(400)
    clicked = await page.evaluate("""(args) => {
        const prefix = 'react-select-' + args.id + '-option-';
        const opts = Array.from(document.querySelectorAll('[role=option]')).filter(o => o.id.startsWith(prefix));
        const target = opts.find(o => o.textContent.trim() === args.v) || opts[0];
        if (!target) return false;
        target.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:0}));
        target.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, button:0}));
        target.dispatchEvent(new MouseEvent('click',     {bubbles:true, button:0}));
        return true;
    }""", {"id": input_id, "v": value})
    await page.wait_for_timeout(500)
    return clicked


async def read(page, ids):
    return await page.evaluate("""(ids) => {
        const out = {};
        for (const id of ids) {
            const el = document.getElementById(id);
            if (!el) { out[id] = '?'; continue; }
            const ctrl = el.closest('.select__control');
            const sv = ctrl ? ctrl.querySelector('.select__single-value') : null;
            out[id] = sv ? sv.textContent.trim() : '';
        }
        return out;
    }""", ids)


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        # Apply playwright-stealth — does this break react-select?
        try:
            from playwright_stealth import Stealth
            await Stealth().apply_stealth_async(page)
            print("playwright-stealth applied")
        except Exception as exc:
            print(f"stealth import failed: {exc}")

        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        ids = ["country", "question_8384146005", "question_8384149005", "question_8384150005"]
        await fill_dropdown(page, "question_8384146005", "Yes")
        await fill_dropdown(page, "question_8384149005", "Yes")
        await fill_dropdown(page, "question_8384150005", "No")
        state = await read(page, ids)
        print("After 3 dropdowns:")
        for k, v in state.items():
            print(f"  {k} = {v!r}")
        await page.wait_for_timeout(2000)
        state2 = await read(page, ids)
        print("After 2s settle:")
        for k, v in state2.items():
            print(f"  {k} = {v!r}")

        await browser.close()

asyncio.run(main())
