"""
Fast headless DOM inspector for Greenhouse phone + gender widgets.
Run: python3 app/scripts/fast_dom_inspect.py
"""
import asyncio, sys, os, json
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from playwright.async_api import async_playwright

JOB_URL = "https://boards.greenhouse.io/monks/jobs/5996484004"

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        await page.goto("https://www.monks.com", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(1)
        await page.goto(JOB_URL, wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(2)

        # Scroll to load everything
        for i in range(5):
            await page.evaluate(f"window.scrollTo(0, {i * 800})")
            await asyncio.sleep(0.2)
        await page.evaluate("window.scrollTo(0,0)")
        await asyncio.sleep(1)

        result = await page.evaluate("""() => {
            const out = {};

            // 1. Phone input + its entire fieldset/container
            const phone = document.querySelector("input[name='phone-number']");
            const phoneCntry = document.querySelector("input[name='phone-country']");
            if (phone) {
                let node = phone;
                for (let i = 0; i < 5; i++) if (node.parentElement) node = node.parentElement;
                out.phone_container = node.outerHTML.substring(0, 6000);
            }
            if (phoneCntry) {
                let node = phoneCntry;
                for (let i = 0; i < 5; i++) if (node.parentElement) node = node.parentElement;
                out.phone_country_container = node.outerHTML.substring(0, 4000);
                
                // Check siblings and nearby visible elements
                const parentEl = phoneCntry.parentElement;
                out.phone_country_siblings = parentEl ? parentEl.outerHTML.substring(0, 3000) : 'N/A';
                
                // Check computed styles
                const cs = window.getComputedStyle(phoneCntry);
                out.phone_country_styles = {
                    display: cs.display,
                    visibility: cs.visibility,
                    opacity: cs.opacity,
                    width: cs.width,
                };
            }

            // 2. Gender/demographic hidden input container
            const demo = document.querySelector("input[name*='demographic']");
            if (demo) {
                let node = demo;
                for (let i = 0; i < 8; i++) if (node.parentElement) node = node.parentElement;
                out.gender_container = node.outerHTML.substring(0, 6000);
            }

            // 3. ALL visible select-like elements (buttons, divs that look like dropdowns)
            const dropdownCandidates = [];
            const allDivs = document.querySelectorAll('div, button, span');
            for (const el of allDivs) {
                const cls = typeof el.className === 'string' ? el.className.toLowerCase() : '';
                const role = el.getAttribute('role') || '';
                if (
                    cls.includes('select') || cls.includes('dropdown') ||
                    cls.includes('combobox') || cls.includes('shell') ||
                    role === 'combobox' || role === 'listbox' ||
                    cls.includes('phone') || cls.includes('country') || cls.includes('flag')
                ) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 20 && r.height > 10) {
                        dropdownCandidates.push({
                            tag: el.tagName,
                            id: el.id,
                            cls: cls.substring(0, 120),
                            role: role,
                            text: el.textContent.trim().substring(0, 60),
                            w: Math.round(r.width),
                            h: Math.round(r.height),
                            html: el.outerHTML.substring(0, 300),
                        });
                    }
                }
            }
            out.dropdown_candidates = dropdownCandidates.slice(0, 30);

            // 4. All select elements (native)
            const selects = [];
            for (const s of document.querySelectorAll('select')) {
                const cs = window.getComputedStyle(s);
                const r = s.getBoundingClientRect();
                selects.push({
                    name: s.name,
                    id: s.id,
                    display: cs.display,
                    visibility: cs.visibility,
                    width: Math.round(r.width),
                    height: Math.round(r.height),
                    options: Array.from(s.options).map(o => o.text).slice(0, 5),
                });
            }
            out.selects = selects;

            return out;
        }""")

        print("\n" + "="*80)
        print("PHONE CONTAINER:")
        print("="*80)
        print(result.get('phone_container', 'NOT FOUND')[:3000])

        print("\n" + "="*80)
        print("PHONE-COUNTRY CONTAINER:")
        print("="*80)
        print(result.get('phone_country_container', 'NOT FOUND')[:2000])

        print("\n" + "="*80)
        print("PHONE-COUNTRY SIBLINGS:")
        print("="*80)
        print(result.get('phone_country_siblings', 'NOT FOUND')[:2000])

        print("\nPHONE-COUNTRY COMPUTED STYLES:", result.get('phone_country_styles'))

        print("\n" + "="*80)
        print("GENDER/DEMOGRAPHIC CONTAINER:")
        print("="*80)
        print(result.get('gender_container', 'NOT FOUND')[:3000])

        print("\n" + "="*80)
        print("NATIVE SELECT ELEMENTS:")
        print("="*80)
        for s in result.get('selects', []):
            print(f"  <select name={s['name']!r} display={s['display']} vis={s['visibility']} {s['width']}x{s['height']}>")
            print(f"    options: {s['options']}")

        print("\n" + "="*80)
        print("VISIBLE DROPDOWN CANDIDATES:")
        print("="*80)
        for d in result.get('dropdown_candidates', []):
            print(f"\n  <{d['tag']} id={d['id']!r} role={d['role']!r} {d['w']}x{d['h']}>")
            print(f"  cls={d['cls'][:80]}")
            print(f"  text={d['text']!r}")
            print(f"  html={d['html'][:200]}")

        await browser.close()
        print("\n[DONE]")

asyncio.run(run())
