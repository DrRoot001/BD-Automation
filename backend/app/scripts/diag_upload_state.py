"""Verify file uploads are visually attached on the Greenhouse form after fill.

Reads the .file-attachment-name (Greenhouse's filename display widget) and the
file input value to confirm both the React state and visible UI agree.
"""
from __future__ import annotations
import asyncio, sys
from pathlib import Path
BACKEND = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND))
from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

URL = "https://job-boards.greenhouse.io/scaleai/jobs/4618065005"
RESUME = str(Path(__file__).resolve().parents[3] / "harmain_ali_butt_resume.pdf")

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context(viewport={"width": 1366, "height": 768})
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        # Scroll the form into view (file inputs render below the fold)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(1500)

        # Find ALL file inputs first
        all_files = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('input[type=file]')).map(f => ({
                id: f.id, name: f.name, files_count: f.files.length,
                visible: f.offsetParent !== null,
            }));
        }""")
        print(f"All file inputs on page: {all_files}\n")

        if not all_files:
            print("No file inputs found — page may need Apply click")
            await browser.close()
            return

        for fi in all_files:
            if not fi['id']:
                continue
            try:
                await page.set_input_files(f"#{fi['id']}", RESUME)
            except Exception as exc:
                print(f"set_input_files #{fi['id']} failed: {exc}")
        await page.wait_for_timeout(2000)

        # Read what file inputs exist NOW (after upload) and their state
        all_after = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('input[type=file]')).map(f => ({
                id: f.id, name: f.name, files_count: f.files.length,
                files_name: f.files.length ? f.files[0].name : '',
                visible: f.offsetParent !== null,
                parent_text: (f.parentElement && f.parentElement.textContent.trim().slice(0, 100)) || '',
            }));
        }""")
        print(f"\nFile inputs AFTER upload: {all_after}\n")

        # Look for any DOM evidence the file is attached anywhere
        attached_evidence = await page.evaluate("""() => {
            const out = [];
            ['.file-attachment-name', '.attachment-name', '.attachment',
             '[class*="filename"]', '[class*="uploaded"]',
             '[class*="attachment"]'].forEach(sel => {
                document.querySelectorAll(sel).forEach(el => {
                    if (el.offsetParent && el.textContent.trim()) {
                        out.push({sel, text: el.textContent.trim().slice(0, 100)});
                    }
                });
            });
            return out;
        }""")
        print(f"Attachment-name evidence: {attached_evidence}\n")

        state = await page.evaluate("""() => {
            const out = {};
            for (const fid of ['resume', 'cover_letter']) {
                const inp = document.getElementById(fid);
                if (!inp) { out[fid] = 'NO INPUT'; continue; }
                // Walk the surrounding wrapper for any element showing the filename
                let wrapper = inp.parentElement;
                let nameNode = null;
                for (let i = 0; i < 6 && wrapper; i++) {
                    nameNode = wrapper.querySelector(
                        '.file-attachment-name, .attachment-name, [class*="attachment"], '
                        + '[class*="filename"], [class*="file-name"]'
                    );
                    if (nameNode && nameNode.textContent.trim().length > 0) break;
                    wrapper = wrapper.parentElement;
                }
                out[fid] = {
                    files_count: inp.files.length,
                    files_name: inp.files.length ? inp.files[0].name : '',
                    visible_name: nameNode ? nameNode.textContent.trim().slice(0, 80) : '<not found>',
                };
            }
            return out;
        }""")
        import json
        print(json.dumps(state, indent=2))

        # Also take a screenshot of just the documents section
        try:
            box = await page.locator("text=/Documents|Resume.*CV/i").first.bounding_box()
            if box:
                screenshot_path = str(Path(BACKEND) / "screenshots" / "diag_upload_state.png")
                await page.screenshot(path=screenshot_path,
                                      clip={"x": 0, "y": box["y"], "width": 1366,
                                            "height": min(500, 768 - box["y"])})
                print(f"\nDocuments-section screenshot: {screenshot_path}")
        except Exception as exc:
            print(f"section screenshot failed: {exc}")

        await browser.close()

asyncio.run(main())
