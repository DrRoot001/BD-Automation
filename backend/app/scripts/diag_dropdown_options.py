"""Find what an OPEN Greenhouse work-auth menu's options look like."""
from __future__ import annotations
import asyncio, sys, json
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

        # Open the work-auth dropdown by clicking the wrapper (.select__control)
        # belonging to question_8384149005.
        # First: dump DOM around the work-auth control BEFORE clicking
        before = await page.evaluate("""() => {
            const input = document.getElementById('question_8384149005');
            if (!input) return null;
            const ctrl = input.closest('.select__control');
            if (!ctrl) return null;
            return {
                ctrl_html_before: ctrl.outerHTML.slice(0, 800),
                ctrl_parent_html: ctrl.parentElement.outerHTML.slice(0, 1200),
            };
        }""")
        print("BEFORE click:")
        print(before.get('ctrl_html_before') if before else None)
        print()

        # Click the wrapper
        clicked = await page.evaluate("""() => {
            const input = document.getElementById('question_8384149005');
            const ctrl = input.closest('.select__control');
            if (!ctrl) return false;
            ctrl.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:0}));
            ctrl.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, button:0}));
            ctrl.dispatchEvent(new MouseEvent('click',     {bubbles:true, button:0}));
            return true;
        }""")
        print(f"clicked: {clicked}")
        await page.wait_for_timeout(1500)

        # Now find ALL elements containing "Yes" exactly as direct text
        opened = await page.evaluate("""() => {
            const report = {
                hits_yes: [],
                full_select_menu_html: '',
                everywhere_with_select_class: [],
            };
            // Look for the menu — it's now an ADJACENT sibling of .select__control after open
            document.querySelectorAll('.select__menu, .select__menu-list, [class*="select__menu"]').forEach(el => {
                report.full_select_menu_html = el.outerHTML.slice(0, 2000);
            });
            // ANY element with text exactly "Yes" or "No" that's currently visible
            document.querySelectorAll('div, span, li, button').forEach(el => {
                if (el.children.length !== 0) return;
                const t = (el.textContent||'').trim();
                if ((t === 'Yes' || t === 'No') && el.offsetParent !== null) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        report.hits_yes.push({
                            tag: el.tagName, role: el.getAttribute('role'),
                            cls: (el.className||'').slice(0,120),
                            text: t,
                            id: el.id,
                            parent_cls: el.parentElement ? (el.parentElement.className||'').slice(0,80) : '',
                        });
                    }
                }
            });
            // Any element whose class contains "menu" or "option"
            document.querySelectorAll('[class*="menu"], [class*="option"]').forEach(el => {
                if (el.offsetParent === null) return;
                if (report.everywhere_with_select_class.length > 10) return;
                report.everywhere_with_select_class.push({
                    cls: (el.className||'').slice(0,120),
                    text: (el.textContent||'').trim().slice(0,40),
                });
            });
            return report;
        }""")
        print("AFTER click (menu open):")
        print(json.dumps(opened, indent=2)[:4000])
        await browser.close()

asyncio.run(main())
