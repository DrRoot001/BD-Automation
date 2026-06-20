"""
DOM Inspector for Greenhouse Application Form
Navigates to the Monks job page, scrolls to load all elements,
then dumps the exact HTML structure of:
1. The phone widget (country-code selector + number input)
2. The gender / demographic custom dropdown
3. ALL custom select-shell widgets

Run: python3 app/scripts/inspect_greenhouse_dom.py
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from playwright.async_api import async_playwright

JOB_URL = "https://boards.greenhouse.io/monks/jobs/5996484004"

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=False)
        page = await browser.new_page()
        
        # Warm up
        print("[*] Warming up domain...")
        await page.goto("https://www.monks.com", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        
        # Navigate to job
        print(f"[*] Navigating to: {JOB_URL}")
        await page.goto(JOB_URL, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3)
        
        # Scroll to load all elements
        print("[*] Scrolling to load all elements...")
        height = await page.evaluate("document.body.scrollHeight")
        pos = 0
        while pos < height:
            pos += 600
            await page.evaluate(f"window.scrollTo(0, {pos})")
            await asyncio.sleep(0.1)
            height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(1)
        
        print("\n" + "="*80)
        print("PHONE WIDGET STRUCTURE")
        print("="*80)
        phone_html = await page.evaluate("""() => {
            // Find the phone-number input
            const phoneInput = document.querySelector("input[name='phone-number']");
            if (!phoneInput) return "NOT FOUND";
            
            // Walk up 6 levels to capture the full widget container
            let node = phoneInput;
            for (let i = 0; i < 6; i++) {
                if (node.parentElement) node = node.parentElement;
            }
            
            // Also find the phone-country hidden input and its surrounding context
            const countryInput = document.querySelector("input[name='phone-country']");
            let countryContext = "NOT FOUND";
            if (countryInput) {
                let cn = countryInput;
                for (let i = 0; i < 5; i++) if (cn.parentElement) cn = cn.parentElement;
                countryContext = cn.outerHTML.substring(0, 3000);
            }
            
            return {
                phoneWidgetHTML: node.outerHTML.substring(0, 5000),
                countryContextHTML: countryContext,
            };
        }""")
        print("Phone widget HTML:")
        if isinstance(phone_html, dict):
            print(phone_html.get('phoneWidgetHTML', ''))
            print("\nCountry input context:")
            print(phone_html.get('countryContextHTML', ''))
        else:
            print(phone_html)
        
        print("\n" + "="*80)
        print("GENDER / DEMOGRAPHIC WIDGET STRUCTURE")
        print("="*80)
        gender_html = await page.evaluate("""() => {
            // The hidden input has name containing 'demographic'
            const demoInput = document.querySelector("input[name*='demographic']");
            if (!demoInput) return "NOT FOUND - trying other selectors...";
            
            let node = demoInput;
            for (let i = 0; i < 8; i++) {
                if (node.parentElement) node = node.parentElement;
            }
            return node.outerHTML.substring(0, 5000);
        }""")
        print(gender_html)
        
        print("\n" + "="*80)
        print("ALL SELECT-SHELL / REACT-SELECT TRIGGERS")
        print("="*80)
        custom_widgets = await page.evaluate("""() => {
            const sels = [
                'div.select-shell-button',
                '.select__control',
                '.react-select__control',
                '[role="combobox"]:not(input):not(select)',
                '.select-shell-button',
                '[class*="select-shell"]',
                '[class*="SelectShell"]',
                '[class*="phone-country"]',
                '[class*="phoneCountry"]',
                'button[data-testid*="phone"]',
                'button[data-testid*="country"]',
            ];
            const results = [];
            const seen = new Set();
            for (const sel of sels) {
                const els = document.querySelectorAll(sel);
                for (const el of els) {
                    if (seen.has(el)) continue;
                    seen.add(el);
                    const r = el.getBoundingClientRect();
                    results.push({
                        selector: sel,
                        tag: el.tagName,
                        id: el.id,
                        className: typeof el.className === 'string' ? el.className.substring(0, 100) : '',
                        text: el.textContent.trim().substring(0, 50),
                        visible: r.width > 5 && r.height > 5,
                        rect: {w: Math.round(r.width), h: Math.round(r.height)},
                        outerHTML: el.outerHTML.substring(0, 200),
                    });
                }
            }
            return results;
        }""")
        print(f"Found {len(custom_widgets)} custom widget elements:")
        for w in custom_widgets:
            print(f"\n  [{w['selector']}] <{w['tag']} id={w['id']!r} class={w['className']!r}>")
            print(f"    text={w['text']!r} visible={w['visible']} size={w['rect']}")
            print(f"    html={w['outerHTML'][:150]}")
        
        print("\n" + "="*80)
        print("ALL VISIBLE FORM INPUTS (including phone-country)")
        print("="*80)
        all_inputs = await page.evaluate("""() => {
            const inputs = document.querySelectorAll('input, select, textarea');
            const results = [];
            for (const inp of inputs) {
                const style = window.getComputedStyle(inp);
                const r = inp.getBoundingClientRect();
                results.push({
                    tag: inp.tagName,
                    type: inp.type || '',
                    name: inp.name || '',
                    id: inp.id || '',
                    display: style.display,
                    visibility: style.visibility,
                    opacity: style.opacity,
                    width: Math.round(r.width),
                    height: Math.round(r.height),
                    value: inp.value ? inp.value.substring(0,30) : '',
                });
            }
            return results.filter(x => 
                x.name && !x.name.startsWith('lt_') && !x.name.startsWith('ft_') 
                && !x.name.startsWith('ga') && x.name !== 'gclid'
                && !x.name.includes('source') && !x.name.includes('campaign')
            );
        }""")
        print("Interesting inputs:")
        for inp in all_inputs:
            vis = f"display={inp['display']} vis={inp['visibility']} op={inp['opacity']} {inp['width']}x{inp['height']}"
            print(f"  <{inp['tag']} type={inp['type']} name={inp['name']!r} id={inp['id']!r}> [{vis}] val={inp['value']!r}")
        
        print("\n" + "="*80)
        print("PHONE COUNTRY BUTTON/DIV (any element near phone-country input)")
        print("="*80)
        phone_country_ctx = await page.evaluate("""() => {
            // Try to find the visible counterpart of the hidden phone-country input
            // Greenhouse typically replaces it with a button or styled div
            const candidates = [
                document.querySelector('[class*="phone"][class*="flag"]'),
                document.querySelector('[class*="phone"][class*="country"]'),
                document.querySelector('[class*="PhoneInput"]'),
                document.querySelector('[class*="react-tel"]'),
                document.querySelector('.iti'),  // intl-tel-input
                document.querySelector('.iti__selected-flag'),
                document.querySelector('[data-testid*="phone"]'),
                document.querySelector('button[class*="phone"]'),
            ];
            const found = [];
            for (const el of candidates) {
                if (el) {
                    found.push({
                        tag: el.tagName,
                        id: el.id,
                        className: typeof el.className === 'string' ? el.className.substring(0,100) : '',
                        html: el.outerHTML.substring(0, 400),
                    });
                }
            }
            return found;
        }""")
        print(f"Phone country button candidates: {len(phone_country_ctx)}")
        for c in phone_country_ctx:
            print(f"  <{c['tag']} id={c['id']!r} class={c['className']!r}>")
            print(f"  {c['html'][:300]}\n")
        
        print("\n[*] Keeping browser open for 30s so you can inspect...")
        await asyncio.sleep(30)
        await browser.close()

asyncio.run(run())
