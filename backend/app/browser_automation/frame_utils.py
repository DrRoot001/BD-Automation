import asyncio
import logging
import time
import os
import datetime
from typing import Optional, Any
from playwright.async_api import Frame, Page, Error as PlaywrightError
from playwright._impl._errors import TargetClosedError

logger = logging.getLogger(__name__)

async def get_live_frame(frame_locator: Any, max_retries: int = 3, page: Optional[Page] = None) -> Optional[Frame]:
    """Resolve a Playwright FrameLocator into a live Frame object, with backoff for TargetClosedError."""
    if not frame_locator:
        return None
        
    for attempt in range(max_retries):
        try:
            # Check if it's already a Frame
            if hasattr(frame_locator, "is_detached"):
                if frame_locator.is_detached():
                    logger.warning(f"[FrameLifecycle {time.time():.3f}] detached on attempt {attempt+1}/{max_retries}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(0.4)
                        continue
                    return None
                if attempt > 0:
                    logger.info(f"[FrameLifecycle {time.time():.3f}] recovered after {attempt} retries")
                return frame_locator
                
            # Assume it's a FrameLocator
            if not hasattr(frame_locator, "owner"):
                logger.error("[FrameLifecycle] frame_locator.owner does not exist (Playwright API change?). Cannot resolve frame.")
                return None
                
            el = await frame_locator.owner.element_handle()
            if el:
                frame = await el.content_frame()
                if frame:
                    if frame.is_detached():
                        logger.warning(f"[FrameLifecycle {time.time():.3f}] detached on attempt {attempt+1}/{max_retries}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(0.4)
                            continue
                        return None
                    if attempt > 0:
                        logger.info(f"[FrameLifecycle {time.time():.3f}] recovered after {attempt} retries")
                    return frame
            return None
        except TargetClosedError as exc:
            if attempt < max_retries - 1:
                logger.warning(f"[FrameLifecycle {time.time():.3f}] TargetClosedError acquiring live frame, retrying ({attempt+1}/{max_retries})")
                await asyncio.sleep(0.4)
                continue
            logger.error(f"[FrameLifecycle {time.time():.3f}] Failed to acquire live frame: {exc}")
            if page:
                try:
                    local_dir = os.getenv("SCREENSHOT_LOCAL_DIR", "./screenshots")
                    os.makedirs(local_dir, exist_ok=True)
                    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                    full_path = os.path.join(local_dir, f"fatal_frame_error_{timestamp}.png")
                    await page.screenshot(path=full_path, full_page=True)
                    logger.info(f"[FrameLifecycle] Captured fatal frame error screenshot to: {full_path}")
                except Exception as capture_exc:
                    logger.warning(f"[FrameLifecycle] Failed to capture diagnostic screenshot: {capture_exc}")
            raise
        except PlaywrightError as exc:
            if "Target closed" in str(exc) or "TargetClosed" in str(exc) or "detached" in str(exc):
                if attempt < max_retries - 1:
                    logger.warning(f"[FrameLifecycle {time.time():.3f}] PlaywrightError acting like TargetClosedError, retrying ({attempt+1}/{max_retries}): {exc}")
                    await asyncio.sleep(0.4)
                    continue
            logger.error(f"[FrameLifecycle {time.time():.3f}] Failed to acquire live frame: {exc}")
            raise
        except Exception as exc:
            logger.error(f"Failed to acquire live frame: {exc}")
            raise
            
    return None

async def wait_for_stability(page: Page, frame_locator: Optional[Any], is_iframe_mode: bool) -> bool:
    """Wait for DOM mutations to settle for 500ms. Returns True if stable, False if failed/timed out."""
    logger.info("Waiting for page/frame stability (500ms no-mutation)...")
    try:
        async def _poll():
            if is_iframe_mode and frame_locator:
                try:
                    await frame_locator.locator("input, select, textarea, button").first.wait_for(state="attached", timeout=5000)
                except Exception:
                    pass
                f = await get_live_frame(frame_locator, max_retries=5, page=page)
                if not f:
                    raise RuntimeError("Could not resolve live frame for stability check")
                target = f
            else:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                target = page
            
            try:
                await target.evaluate("""() => {
                    return new Promise(resolve => {
                        let timer = setTimeout(resolve, 500);
                        const observer = new MutationObserver(() => {
                            clearTimeout(timer);
                            timer = setTimeout(() => { observer.disconnect(); resolve(); }, 500);
                        });
                        observer.observe(document.body, { childList: true, subtree: true, attributes: true });
                    });
                }""")
            except Exception:
                pass

        await asyncio.wait_for(_poll(), timeout=10.0)
        return True
    except Exception as e:
        logger.warning(f"Stability wait timed out or failed: {e}")
        await asyncio.sleep(1.0)
        return False
