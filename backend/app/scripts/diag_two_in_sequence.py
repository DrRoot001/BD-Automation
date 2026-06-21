"""Fill 3 dropdowns in sequence — does each one persist?"""
from __future__ import annotations
import asyncio, sys
from pathlib import Path
BACKEND = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND))
from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

URL = "https://job-boards.greenhouse.io/scaleai/jobs/4618065005"

async def fill_dropdown(page, input_id: str, value: str):
    """Native-event fill of a single react-select dropdown — WITH typing."""
    # Open
    await page.evaluate("""(args) => {
        const el = document.getElementById(args.id);
        const ctrl = el.closest('.select__control');
        ctrl.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:0}));
        ctrl.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, button:0}));
    }""", {"id": input_id, "v": value})
    await page.wait_for_timeout(500)
    # Focus + type (this is what production does)
    await page.locator(f"#{input_id}").focus()
    await page.keyboard.type(value, delay=30)
    await page.wait_for_timeout(400)
    # Click matching option via mousedown
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

async def read_dropdown(page, input_id: str) -> str:
    return await page.evaluate("""(id) => {
        const el = document.getElementById(id);
        const ctrl = el.closest('.select__control');
        const sv = ctrl.querySelector('.select__single-value');
        return sv ? sv.textContent.trim() : '';
    }""", input_id)

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # IDs of work-auth and sponsorship and in-person
        in_person  = "question_8384146005"
        work_auth  = "question_8384149005"
        sponsorship = "question_8384150005"

        print("Filling 3 dropdowns in sequence...")
        c1 = await fill_dropdown(page, in_person, "Yes")
        v1_immediate = await read_dropdown(page, in_person)
        print(f"  in_person  -> clicked={c1}, immediate read='{v1_immediate}'")

        c2 = await fill_dropdown(page, work_auth, "Yes")
        v1_after_2 = await read_dropdown(page, in_person)
        v2_immediate = await read_dropdown(page, work_auth)
        print(f"  work_auth  -> clicked={c2}, immediate read='{v2_immediate}'")
        print(f"    in_person STILL = '{v1_after_2}'")

        c3 = await fill_dropdown(page, sponsorship, "No")
        v1_after_3 = await read_dropdown(page, in_person)
        v2_after_3 = await read_dropdown(page, work_auth)
        v3_immediate = await read_dropdown(page, sponsorship)
        print(f"  sponsorship -> clicked={c3}, immediate read='{v3_immediate}'")
        print(f"    in_person STILL = '{v1_after_3}'")
        print(f"    work_auth STILL = '{v2_after_3}'")

        print()
        print("=== After 2s wait ===")
        await page.wait_for_timeout(2000)
        print(f"  in_person   = '{await read_dropdown(page, in_person)}'")
        print(f"  work_auth   = '{await read_dropdown(page, work_auth)}'")
        print(f"  sponsorship = '{await read_dropdown(page, sponsorship)}'")

        await browser.close()

asyncio.run(main())
