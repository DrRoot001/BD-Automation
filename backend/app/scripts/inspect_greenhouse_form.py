"""Diagnostic — dump the DOM structure of the ScaleAI Greenhouse form so we
can see exactly what dropdowns and file inputs look like under the hood.
"""
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
        await page.wait_for_timeout(3000)
        # Scroll the form into view to trigger lazy fields
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(1500)

        info = await page.evaluate("""
() => {
    const report = { selects: [], files: [], react_selects: [], comboboxes: [], all_inputs: [] };
    // 1. Native selects
    document.querySelectorAll('select').forEach(s => {
        report.selects.push({ name: s.name, id: s.id, options: Array.from(s.options).slice(0,5).map(o=>o.text) });
    });
    // 2. File inputs
    document.querySelectorAll('input[type=file]').forEach(i => {
        report.files.push({ name: i.name, id: i.id, hidden: i.offsetParent === null,
            parent_class: i.parentElement ? i.parentElement.className : '',
            parent_tag: i.parentElement ? i.parentElement.tagName : '',
        });
    });
    // 3. react-select markers
    document.querySelectorAll('[class*="react-select"], [class*="select__control"], [class*="Select__control"]').forEach(el => {
        report.react_selects.push({ tag: el.tagName, classes: el.className.slice(0,150) });
    });
    // 4. ARIA comboboxes (modern react-select)
    document.querySelectorAll('[role=combobox], [aria-haspopup=listbox]').forEach(el => {
        const labelEl = el.closest('label') || document.querySelector(`label[for="${el.id}"]`);
        report.comboboxes.push({
            id: el.id, name: el.getAttribute('name'),
            role: el.getAttribute('role'),
            aria_haspopup: el.getAttribute('aria-haspopup'),
            aria_labelledby: el.getAttribute('aria-labelledby'),
            classes: (el.className||'').slice(0,150),
            visible: el.offsetParent !== null,
            nearby_label: labelEl ? labelEl.textContent.slice(0,80) : null,
        });
    });
    // 5. Sample of ALL inputs to see what's there
    document.querySelectorAll('input, textarea, button').forEach(el => {
        if (report.all_inputs.length > 30) return;
        report.all_inputs.push({
            tag: el.tagName, type: el.type || el.getAttribute('type'),
            id: el.id, name: el.name,
            role: el.getAttribute('role'),
            aria_label: el.getAttribute('aria-label'),
            placeholder: el.placeholder,
            classes: (el.className||'').slice(0,80),
            visible: el.offsetParent !== null,
        });
    });
    return report;
}
""")
        print(json.dumps(info, indent=2)[:6000])
        await browser.close()

asyncio.run(main())
