import os
import datetime
import logging
from playwright.async_api import Page
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

async def capture_and_store_screenshot(page: Page, application_id: str) -> str:
    local_dir = os.getenv("SCREENSHOT_LOCAL_DIR", "./screenshots")
    os.makedirs(local_dir, exist_ok=True)

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{application_id}_{timestamp}.png"
    full_path = os.path.join(local_dir, filename)

    await page.screenshot(path=full_path, full_page=True)
    logger.info(f"Screenshot saved to: {full_path}")
    return full_path
