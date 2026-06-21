import pytest
import asyncio
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from playwright.async_api import async_playwright
from backend.app.browser_automation.adapters.greenhouse import GreenhouseAdapter
from backend.app.browser_automation.frame_utils import get_live_frame

@pytest.mark.asyncio
async def test_greenhouse_iframe_destroy_recreate_cycle():
    """
    Regression test to ensure we survive a Greenhouse-style iframe swap mid-operation.
    This simulates an Apply click that injects an iframe, which then reloads 1 second later.
    """
    fixture_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "fixtures", "frame_swap_main.html"))
    file_url = f"file://{fixture_path}"
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        adapter = GreenhouseAdapter()
        
        # 1. Navigate and let the adapter click Apply
        await page.goto(file_url)
        
        # We manually trigger the adapter's navigation phase which clicks "Apply Now"
        # and proactively waits for the iframe injection.
        await adapter.navigate_to_application(page, file_url)
        
        assert adapter._iframe_mode is True
        assert adapter._frame_locator is not None
        
        # 2. We don't need to manually wait and get the frame, because
        # navigate_to_application already calls wait_for_stability() which handles the swap!
        # wait_for_stability will poll until the new iframe is ready.
        
        # 3. Simulate AgentLoop getting the live frame after adapter returns
        # This confirms that wait_for_stability successfully handled the chaotic swap
        frame = await get_live_frame(adapter._frame_locator, max_retries=5, page=page)
        
        assert frame is not None
        assert not frame.is_detached()
        
        # 4. Verify we can interact with the new reloaded frame content
        # It should contain the inputs from frame_content.html
        count = await adapter._frame_locator.locator("input#first_name").count()
        if count == 0:
            await page.screenshot(path="test_debug_screenshot.png", full_page=True)
            print("Captured test_debug_screenshot.png")
        assert count > 0
        
        await browser.close()
