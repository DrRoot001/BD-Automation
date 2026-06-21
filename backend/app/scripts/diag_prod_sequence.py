"""Mimic the EXACT production sequence: text fields first, then dropdowns,
checking dropdown values after EACH step. Find which operation breaks the
commit chain.
"""
from __future__ import annotations
import asyncio, sys
from pathlib import Path
BACKEND = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND))
from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

URL = "https://job-boards.greenhouse.io/scaleai/jobs/4618065005"


async def fill_dropdown(page, input_id: str, value: str):
    """Use the proven mousedown+mouseup+click approach."""
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


async def read_all_dropdowns(page) -> dict:
    return await page.evaluate("""() => {
        const out = {};
        const inputs = ['country', 'question_8384146005', 'question_8384149005',
                        'question_8384150005', 'gender', 'hispanic_ethnicity',
                        'veteran_status', 'disability_status'];
        for (const id of inputs) {
            const el = document.getElementById(id);
            if (!el) { out[id] = 'NO ELEMENT'; continue; }
            const ctrl = el.closest('.select__control');
            const sv = ctrl ? ctrl.querySelector('.select__single-value') : null;
            out[id] = sv ? sv.textContent.trim() : '';
        }
        return out;
    }""")


def snapshot(label, state):
    print(f"--- {label} ---")
    for k, v in state.items():
        print(f"  {k:<30} = {v!r}")


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Step 1: fill text fields with NATIVE-SETTER trick (like production does)
        print("\n=== STEP 1: native-setter text fills ===")
        for sel, val in [
            ("#first_name", "Harmain"),
            ("#last_name", "Ali Butt"),
            ("#email", "harmain.ali.butt@gmail.com"),
        ]:
            await page.evaluate(
                """({sel, val}) => {
                    const el = document.querySelector(sel);
                    if (!el) return;
                    const proto = window.HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input',  {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                }""",
                {"sel": sel, "val": val},
            )
        snapshot("after native-setter text fills", await read_all_dropdowns(page))

        # Step 2: fill file inputs (like production)
        print("\n=== STEP 2: file uploads ===")
        resume_path = str(Path(__file__).resolve().parents[3] / "harmain_ali_butt_resume.pdf")
        if Path(resume_path).is_file():
            await page.set_input_files("#resume", resume_path)
            await page.evaluate(
                "sel => { const el = document.querySelector(sel);"
                "if (el) { el.dispatchEvent(new Event('input', {bubbles:true}));"
                "el.dispatchEvent(new Event('change', {bubbles:true})); } }",
                "#resume",
            )
            await page.set_input_files("#cover_letter", resume_path)
            await page.evaluate(
                "sel => { const el = document.querySelector(sel);"
                "if (el) { el.dispatchEvent(new Event('input', {bubbles:true}));"
                "el.dispatchEvent(new Event('change', {bubbles:true})); } }",
                "#cover_letter",
            )
        snapshot("after file uploads", await read_all_dropdowns(page))

        # Step 3: fill dropdowns one by one, reading state after each
        print("\n=== STEP 3: dropdowns ===")
        dropdowns = [
            ("country", "United States"),
            ("question_8384146005", "Yes"),
            ("question_8384149005", "Yes"),
            ("question_8384150005", "No"),
            ("gender", "Prefer not to answer"),
            ("hispanic_ethnicity", "Prefer not to answer"),
            ("veteran_status", "Prefer not to answer"),
            ("disability_status", "Prefer not to answer"),
        ]
        for did, dval in dropdowns:
            ok = await fill_dropdown(page, did, dval)
            state = await read_all_dropdowns(page)
            print(f"  filled {did}='{dval}' -> clicked={ok}, all_state:")
            for k, v in state.items():
                marker = "  <" if k == did else "    "
                print(f"    {marker} {k:<30} = {v!r}")

        # Final after settle
        print("\n=== FINAL STATE after 3s settle ===")
        await page.wait_for_timeout(3000)
        snapshot("FINAL", await read_all_dropdowns(page))

        await browser.close()


asyncio.run(main())
