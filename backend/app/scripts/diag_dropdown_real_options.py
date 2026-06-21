"""Open each dropdown and dump its REAL option text."""
from __future__ import annotations
import asyncio, sys
from pathlib import Path
BACKEND = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND))
from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

URL = "https://job-boards.greenhouse.io/scaleai/jobs/4618065005"

DROPDOWNS = [
    ("country", "Country"),
    ("question_8384146005", "in-person"),
    ("question_8384147005", "non-compete"),
    ("question_8384149005", "work-auth"),
    ("question_8384150005", "sponsorship"),
    ("gender", "Gender"),
    ("hispanic_ethnicity", "Hispanic"),
    ("veteran_status", "Veteran"),
    ("disability_status", "Disability"),
]

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        for did, label in DROPDOWNS:
            print(f"\n=== {label} (#{did}) ===")
            # Open the dropdown
            opened = await page.evaluate("""(id) => {
                const el = document.getElementById(id);
                if (!el) return false;
                const ctrl = el.closest('.select__control');
                if (!ctrl) return false;
                ctrl.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:0}));
                ctrl.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, button:0}));
                return true;
            }""", did)
            await page.wait_for_timeout(700)
            opts = await page.evaluate("""(id) => {
                const prefix = 'react-select-' + id + '-option-';
                const all = Array.from(document.querySelectorAll('[role=option]'));
                const scoped = all.filter(o => o.id && o.id.startsWith(prefix));
                return scoped.map(o => ({id: o.id, text: o.textContent.trim()}));
            }""", did)
            print(f"  opened={opened}, options ({len(opts)}):")
            for o in opts[:30]:
                print(f"    {o['id']:<50} = {o['text']!r}")
            if len(opts) > 30:
                print(f"    ... and {len(opts)-30} more")
            # Close
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)

        await browser.close()

asyncio.run(main())
