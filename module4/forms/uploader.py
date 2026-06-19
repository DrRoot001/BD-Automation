import os
import logging
import httpx
import tempfile
from playwright.async_api import Page

logger = logging.getLogger(__name__)

async def upload_file(page: Page, selector: str, file_url: str) -> bool:
    try:
        # Handle local files directly
        if not (file_url.startswith("http://") or file_url.startswith("https://")):
            if os.path.exists(file_url):
                await page.set_input_files(selector, file_url)
                return True
            else:
                raise FileNotFoundError(f"Local file not found: {file_url}")

        async with httpx.AsyncClient() as client:
            response = await client.get(file_url)
            response.raise_for_status()
            
            # Extract filename from URL or header
            filename = file_url.split("/")[-1] or "upload.pdf"
            
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_file_path = os.path.join(temp_dir, filename)
                with open(temp_file_path, "wb") as f:
                    f.write(response.content)
                
                await page.set_input_files(selector, temp_file_path)
                return True
    except Exception as e:
        logger.error(f"File upload failed for selector '{selector}': {e}")
        return False
