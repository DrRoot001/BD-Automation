#!/usr/bin/env python3
"""Read-only: open the consent react-selects and dump their real options +
test whether 'Yes' commits. No submit, no LLM."""
import asyncio, sys
sys.path.insert(0, "backend")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

URL = "https://job-boards.greenhouse.io/vercel/jobs/5999792004"
FIELDS = ["question_17855262004", "question_17855263004"]


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        pg = await (await b.new_context()).new_page()
        await pg.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        for fid in FIELDS:
            print(f"\n=== {fid} ===")
            sel = f'[id="{fid}"]'
            loc = pg.locator(sel).first
            if await loc.count() == 0:
                print("  NOT FOUND"); continue
            # label
            lbl = await loc.evaluate("""el => {
                const l = el.closest('.application-question,[class*=question],fieldset,.field');
                return l ? (l.textContent||'').slice(0,90).trim() : '';
            }""")
            print(f"  label: {lbl!r}")
            # open the dropdown and read options
            try:
                await loc.scroll_into_view_if_needed(timeout=3000)
                await loc.click(timeout=3000)
                await pg.wait_for_timeout(600)
                opts = await pg.evaluate("""() => {
                    const out=[];
                    document.querySelectorAll(
                        '.select__menu .select__option,.react-select__menu .react-select__option,'
                      +'[role="listbox"] [role="option"],.select__menu [role="option"]'
                    ).forEach(e=>{const t=(e.textContent||'').trim(); if(t)out.push(t);});
                    return out;
                }""")
                print(f"  options: {opts}")
                # try to click the 'Yes'/affirmative option
                clicked = await pg.evaluate("""() => {
                    const nodes=document.querySelectorAll(
                        '.select__menu .select__option,[role="listbox"] [role="option"],.select__menu [role="option"]');
                    for(const n of nodes){const t=(n.textContent||'').trim().toLowerCase();
                        if(t==='yes'||t.startsWith('i ')||t.startsWith('yes')){n.click();return n.textContent.trim();}}
                    return null;
                }""")
                await pg.wait_for_timeout(400)
                committed = await loc.evaluate("""el => {
                    const ctrl=el.closest('[class*="select__control"],[class*="-control"]');
                    if(ctrl){const sv=ctrl.querySelector('[class*="singleValue"],[class*="-singleValue"],[class*="multi-value"]');
                        return sv?sv.textContent.trim():'';}
                    return el.value||'';
                }""")
                print(f"  clicked option: {clicked!r}")
                print(f"  committed value after click: {committed!r}")
            except Exception as e:
                print(f"  ERROR: {e}")
        await b.close()

asyncio.run(main())
