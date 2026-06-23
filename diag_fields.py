#!/usr/bin/env python3
"""Read-only diagnostic: inspect the Vercel Greenhouse form's field types.
No submit, no LLM. Tells us what widget the consent / demographic fields
actually are, so we know why 'Yes' doesn't commit."""
import asyncio, sys, os
sys.path.insert(0, "backend")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

URL = "https://job-boards.greenhouse.io/vercel/jobs/5999792004"
TARGETS = ["question_17855262004", "question_17855263004",  # consent
           "4015790004", "4015785004", "4015780004"]         # gender, orientation, veteran


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        pg = await (await b.new_context()).new_page()
        await pg.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        info = await pg.evaluate("""(ids) => {
            const out = {};
            for (const id of ids) {
                let el = document.getElementById(id);
                if (!el) {
                    // react-select stores the real id on a hidden input or container
                    el = document.querySelector('[id="'+id+'"]') ||
                         document.querySelector('#'+CSS.escape(id));
                }
                if (!el) { out[id] = 'NOT FOUND'; continue; }
                const tag = el.tagName.toLowerCase();
                const type = el.type || '';
                const role = el.getAttribute('role') || '';
                // nearby control container for react-select
                const ctrl = el.closest('[class*="select__control"],[class*="-control"]');
                const isReactSelect = !!ctrl || !!document.querySelector('[id="'+id+'"] + div [class*="select__"]');
                out[id] = {tag, type, role, isReactSelect, required: el.required||el.getAttribute('aria-required')};
            }
            return out;
        }""", TARGETS)
        for k, v in info.items():
            print(f"{k}: {v}")
        await b.close()

asyncio.run(main())
