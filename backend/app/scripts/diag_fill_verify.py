"""Drill down: fill First Name on real form, read it back at intervals."""
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
        await page.wait_for_timeout(3000)

        # 1. How many elements match #first_name?
        count = await page.locator("#first_name").count()
        print(f"#first_name count: {count}")
        # All variants
        info = await page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('#first_name, input[name=first_name], input[id=first_name]').forEach(el => {
                out.push({
                    tag: el.tagName, id: el.id, name: el.name, type: el.type,
                    visible: el.offsetParent !== null,
                    width: el.getBoundingClientRect().width,
                    height: el.getBoundingClientRect().height,
                });
            });
            return out;
        }""")
        print(f"  elements: {info}")

        # 2. Plain fill
        await page.locator("#first_name").first.fill("Harmain")
        v1 = await page.locator("#first_name").first.input_value()
        print(f"after fill: input_value='{v1}'")
        # Wait 5 seconds and re-read
        await page.wait_for_timeout(5000)
        v2 = await page.locator("#first_name").first.input_value()
        print(f"5s later:   input_value='{v2}'")

        # 3. Now click a dropdown to see if that clears First Name
        await page.locator("#first_name").first.fill("Harmain")
        v3 = await page.locator("#first_name").first.input_value()
        print(f"refilled:   input_value='{v3}'")
        await page.evaluate("""() => {
            const ctrl = document.querySelector('#country').closest('.select__control');
            ctrl.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:0}));
            ctrl.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, button:0}));
        }""")
        await page.wait_for_timeout(500)
        v4 = await page.locator("#first_name").first.input_value()
        print(f"after country dropdown open: input_value='{v4}'")

        # 4. Click "United States" option
        clicked = await page.evaluate("""() => {
            const opts = document.querySelectorAll('.select__menu .select__option');
            for (const o of opts) {
                if (o.textContent.trim() === 'United States') {
                    o.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:0}));
                    o.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, button:0}));
                    o.dispatchEvent(new MouseEvent('click',     {bubbles:true, button:0}));
                    return true;
                }
            }
            return false;
        }""")
        print(f"clicked US option: {clicked}")
        await page.wait_for_timeout(800)

        # 5. Read both First Name AND Country display
        v5 = await page.locator("#first_name").first.input_value()
        cv = await page.evaluate("""() => {
            const ctrl = document.querySelector('#country').closest('.select__control');
            const v = ctrl.querySelector('.select__single-value');
            return v ? v.textContent.trim() : '';
        }""")
        print(f"after option click: first_name='{v5}', country='{cv}'")

        await browser.close()

asyncio.run(main())
