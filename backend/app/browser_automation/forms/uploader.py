import os
import logging
import httpx
import tempfile
from playwright.async_api import Page

logger = logging.getLogger(__name__)

async def _post_upload_react_sync(page: Page, selector: str) -> None:
    """After set_input_files, force-dispatch input + change events on the
    target so React-controlled file uploaders (Greenhouse, Ashby, Workday)
    actually commit the file into their internal state. Playwright's
    set_input_files fires a native change but React's synthetic event layer
    occasionally misses it on 1px hidden inputs.
    """
    try:
        await page.evaluate(
            """sel => {
                const el = document.querySelector(sel);
                if (!el) return;
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            }""",
            selector,
        )
    except Exception as exc:
        logger.debug(f"post-upload event dispatch failed for {selector!r}: {exc}")


async def upload_file(page: Page, selector: str, file_url: str) -> bool:
    try:
        # Handle local files directly. Playwright's set_input_files already
        # dispatches a native change event — DO NOT also dispatch synthetic
        # input/change events afterward. On Greenhouse, the extra synthetic
        # events trigger a form-wide React rerender that resets all custom
        # react-select widgets back to placeholder, even though they were
        # previously committed.
        if not (file_url.startswith("http://") or file_url.startswith("https://")):
            if os.path.exists(file_url):
                await page.set_input_files(selector, file_url)
                return True
            else:
                raise FileNotFoundError(f"Local file not found: {file_url}")

        async with httpx.AsyncClient() as client:
            response = await client.get(file_url)
            response.raise_for_status()
            filename = file_url.split("/")[-1] or "upload.pdf"
            
            # Strip version suffixes like _v38, -v38, _version38, etc. at the end of the base name
            import re
            base, ext = os.path.splitext(filename)
            cleaned_base = re.sub(r'[_-]v(?:ersion)?_?\d+$', '', base, flags=re.IGNORECASE)
            cleaned_base = re.sub(r'v\d+$', '', cleaned_base, flags=re.IGNORECASE)
            cleaned_base = cleaned_base.rstrip('_-')
            filename = cleaned_base + ext

            with tempfile.TemporaryDirectory() as temp_dir:
                temp_file_path = os.path.join(temp_dir, filename)
                with open(temp_file_path, "wb") as f:
                    f.write(response.content)
                await page.set_input_files(selector, temp_file_path)
                return True
    except Exception as e:
        logger.error(f"File upload failed for selector '{selector}': {e}")
        return False
