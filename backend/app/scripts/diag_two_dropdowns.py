"""Drill into why work-auth and sponsorship dropdowns won't commit."""
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

        # Pick the work-auth dropdown alone — no other interaction
        WORK_AUTH = "#question_8384149005"

        info_before = await page.evaluate("""(sel) => {
            const el = document.querySelector(sel);
            if (!el) return {found: false};
            const ctrl = el.closest('.select__control');
            return {
                found: true, ctrl_class: ctrl.className,
                placeholder: ctrl.querySelector('.select__placeholder') ? ctrl.querySelector('.select__placeholder').textContent : null,
                single_value: ctrl.querySelector('.select__single-value') ? ctrl.querySelector('.select__single-value').textContent : null,
                ctrl_html: ctrl.outerHTML.slice(0, 400),
            };
        }""", WORK_AUTH)
        print("BEFORE any interaction:")
        print(f"  ctrl_class: {info_before['ctrl_class']}")
        print(f"  placeholder: {info_before['placeholder']}")
        print(f"  single_value: {info_before['single_value']}")
        print()

        # Open dropdown via mousedown
        await page.evaluate("""(sel) => {
            const el = document.querySelector(sel);
            const ctrl = el.closest('.select__control');
            ctrl.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:0}));
            ctrl.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, button:0}));
        }""", WORK_AUTH)
        await page.wait_for_timeout(700)

        # Type 'Yes'
        await page.locator(WORK_AUTH).focus()
        await page.keyboard.type("Yes", delay=40)
        await page.wait_for_timeout(500)

        # Show option DOM
        opts = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('.select__menu [role="option"], .select__option')).map(o => ({
                id: o.id, text: o.textContent.trim(),
                cls: o.className.slice(0,80), visible: o.offsetParent !== null
            }));
        }""")
        print(f"options after typing 'Yes':")
        for o in opts: print(f"  {o}")
        print()

        # Click Yes via mousedown
        clicked = await page.evaluate("""() => {
            const opts = Array.from(document.querySelectorAll('.select__menu [role="option"], .select__option'));
            const target = opts.find(o => o.textContent.trim() === 'Yes' && o.offsetParent !== null);
            if (!target) return null;
            target.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:0}));
            target.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, button:0}));
            target.dispatchEvent(new MouseEvent('click',     {bubbles:true, button:0}));
            return {id: target.id, text: target.textContent.trim()};
        }""")
        print(f"clicked option: {clicked}")
        await page.wait_for_timeout(800)

        info_after = await page.evaluate("""(sel) => {
            const el = document.querySelector(sel);
            const ctrl = el.closest('.select__control');
            return {
                ctrl_class: ctrl.className,
                placeholder: ctrl.querySelector('.select__placeholder') ? ctrl.querySelector('.select__placeholder').textContent : null,
                single_value: ctrl.querySelector('.select__single-value') ? ctrl.querySelector('.select__single-value').textContent : null,
                input_value: el.value,
                ctrl_html: ctrl.outerHTML.slice(0, 500),
            };
        }""", WORK_AUTH)
        print()
        print("AFTER click:")
        print(f"  ctrl_class: {info_after['ctrl_class']}")
        print(f"  placeholder: {info_after['placeholder']}")
        print(f"  single_value: {info_after['single_value']}")
        print(f"  input_value: {info_after['input_value']}")
        print(f"  ctrl_html: {info_after['ctrl_html'][:300]}")

        await browser.close()

asyncio.run(main())
