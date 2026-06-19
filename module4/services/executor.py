import os
import time
import logging
import asyncio
import redis.asyncio as redis # Assuming RateLimiter will use Redis
import httpx # For state machine transitions, if not passed in

from typing import Optional, Dict, Literal
from playwright.async_api import async_playwright, Page, BrowserContext
from dotenv import load_dotenv

from ..browser import BrowserContextManager
from ..adapters import get_adapter, BasePlatformAdapter
from ..forms import detect_form
from ..captcha import CaptchaService

from .models import ApplicationPackage, ApplicationResult
from .screenshot import capture_and_store_screenshot
from .state_machine import transition_status

load_dotenv()
logger = logging.getLogger(__name__)

# Basic RateLimiter implementation based on Redis
class RateLimiter:
    def __init__(self):
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._redis = None
        self.max_requests_per_hour = 60 # Example: 60 requests per hour per candidate/platform

    async def _get_redis(self):
        if self._redis is None:
            kwargs = {}
            if "rediss://" in self.redis_url:
                kwargs["ssl_cert_reqs"] = "none"
            self._redis = redis.from_url(self.redis_url, **kwargs)
        return self._redis

    async def check_and_increment(self, platform: str, candidate_id: str) -> bool:
        redis_client = await self._get_redis()
        key = f"rate_limit:{candidate_id}:{platform}"
        # Increment and set expiry for 1 hour if new
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, 3600) # Expire in 1 hour
        
        if count > self.max_requests_per_hour:
            logger.warning(f"Rate limit exceeded for candidate {candidate_id} on platform {platform}. Count: {count}")
            return False
        return True

class ApplicationExecutor:
    async def execute(self, package: ApplicationPackage, retry_count: int = 0) -> ApplicationResult:
        start_time = time.time()
        screenshot_path: Optional[str] = None
        confirmation_text: Optional[str] = None
        error_message: Optional[str] = None
        status: Literal["SUBMITTED", "FORM_COMPLETED", "FAILED", "CAPTCHA_FAILED", "RATE_LIMITED", "BLOCKED"] = "FAILED"
        
        context_mgr: Optional[BrowserContextManager] = None
        context: Optional[BrowserContext] = None
        
        try:
            # STEP 1: Check rate limit
            rate_limiter = RateLimiter()
            allowed = await rate_limiter.check_and_increment(package.platform, package.candidate_id)
            if not allowed:
                status = "RATE_LIMITED"
                error_message = "Rate limit exceeded"
                logger.warning(f"Application {package.application_id} failed due to rate limit.")
                return ApplicationResult(
                    application_id=package.application_id,
                    status=status,
                    execution_time_seconds=time.time() - start_time,
                    retry_count=retry_count,
                    error_message=error_message
                )

            # STEP 2: Get adapter
            adapter: BasePlatformAdapter = get_adapter(package.platform)

            # STEP 3: Get browser context
            context_mgr = BrowserContextManager()
            context = await context_mgr.get_context(package.candidate_id, package.platform)
            page = await context.new_page()

            # STEP 4: Transition to APPLICATION_STARTED
            await transition_status(package.application_id, "APPLICATION_STARTED")

            # STEP 5: Navigate to job URL
            await adapter.navigate_to_application(page, package.job_url)

            # STEP 6: Detect form
            form = await detect_form(page, container_selector=adapter.container_selector)

            # STEP 6.5: Call M3 prepare-package endpoint
            logger.info("[M4] Calling M3 prepare-package endpoint")
            needs_cover_letter = any("cover" in field.label.lower() for field in form.fields if field.field_type == "file")
            
            exclude_keywords = ["first name", "last name", "email", "phone", "resume", "cover letter", "cv"]
            screening_questions = []
            for field in form.fields:
                label_lower = field.label.lower()
                if any(kw in label_lower for kw in exclude_keywords):
                    continue
                if field.field_type in ("text", "textarea", "select", "radio", "checkbox"):
                    screening_questions.append(field.label)
            
            api_base = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api")
            prepare_url = f"{api_base}/applications/prepare-package"
            payload = {
                "candidate_id": package.candidate_id,
                "job_id": package.job_id,
                "needs_cover_letter": needs_cover_letter,
                "screening_questions": screening_questions
            }
            
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    m3_resp = await client.post(prepare_url, json=payload)
                    if m3_resp.status_code == 200:
                        m3_data = m3_resp.json()
                        logger.info(f"M3 prepare-package response: {m3_data}")
                        
                        # TEMPORARY FOR TEST: Force should_apply to True to verify filler/upload pipelines
                        if False and not m3_data.get("should_apply", True):
                            logger.warning(
                                f"[M4] M3 returned should_apply=False "
                                f"(score: {m3_data.get('score', 'N/A')}). Abandoning."
                            )
                            await transition_status(
                                package.application_id, "ANALYZED",
                                {"reason": "score_below_threshold"}
                            )
                            if context_mgr and context:
                                await context_mgr.destroy_context(context)
                            return ApplicationResult(
                                application_id=package.application_id,
                                status="FAILED",
                                error_message=f"Abandoned: score below threshold",
                                execution_time_seconds=time.time() - start_time,
                                retry_count=retry_count
                            )

                        if m3_data.get("resume_pdf_url"):
                            package.resume_url = m3_data["resume_pdf_url"]
                        if m3_data.get("cover_letter_pdf_url"):
                            package.cover_letter_url = m3_data["cover_letter_pdf_url"]
                        if m3_data.get("screening_answers"):
                            if not package.screening_answers:
                                package.screening_answers = {}
                            package.screening_answers.update(m3_data["screening_answers"])
                    else:
                        logger.error(f"M3 prepare-package failed with status {m3_resp.status_code}: {m3_resp.text}")
            except Exception as e:
                logger.error(f"Error calling M3 prepare-package endpoint: {e}")

            # STEP 7: Fill form
            fill_success = await adapter.fill_application(
                page, package.candidate_profile,
                package.resume_url, package.cover_letter_url,
                package.screening_answers
            )
            if not fill_success:
                raise Exception("Form fill incomplete")

            # STEP 8: Handle captcha if detected
            dry_run = os.getenv("DRY_RUN_NO_SUBMIT", "false").lower() == "true"
            provider = os.getenv("CAPTCHA_PROVIDER", "2captcha").lower()
            captcha_key = os.getenv(
                "TWO_CAPTCHA_API_KEY" if provider == "2captcha" else "ANTI_CAPTCHA_API_KEY", ""
            ) or ""
            # A placeholder/missing key means no real solving capability is configured.
            key_configured = bool(captcha_key) and not captcha_key.lower().startswith("your_")

            if form.has_captcha:
                if dry_run or not key_configured:
                    # In dry-run we never submit, so the captcha token isn't needed; and
                    # without a real solver key there's nothing to call. Skip gracefully
                    # instead of burning a failed solve / crashing the run.
                    logger.warning(
                        f"[M4] Captcha detected ({form.captcha_type}) — skipping solve "
                        f"(dry_run={dry_run}, solver_key_configured={key_configured})."
                    )
                else:
                    captcha_svc = CaptchaService(provider=provider)
                    solution = await captcha_svc.solve(page, form.captcha_type)
                    if not solution.success:
                        status = "CAPTCHA_FAILED"
                        error_message = f"Captcha solving failed: {form.captcha_type}"
                        screenshot_path = await capture_and_store_screenshot(page, package.application_id)
                        logger.warning(f"Application {package.application_id} failed due to CAPTCHA.")
                        return ApplicationResult(
                            application_id=package.application_id,
                            status=status,
                            screenshot_url=screenshot_path,
                            error_message=error_message,
                            execution_time_seconds=time.time() - start_time,
                            retry_count=retry_count
                        )

            # STEP 9: Transition to FORM_COMPLETED
            await transition_status(package.application_id, "FORM_COMPLETED")

            # STEP 10: Submit
            dry_run = os.getenv("DRY_RUN_NO_SUBMIT", "false").lower() == "true"
            if dry_run:
                logger.info("[DRY RUN] Bypassing real submission submit click.")
                submitted = True
                verified = True
                confirmation_text = "DRY RUN SUCCESS (no submit)"
            else:
                submitted = await adapter.submit(page)
                if not submitted:
                    raise Exception("Submission click failed")

            # STEP 11: Verify success
            if not dry_run:
                verified, confirmation_text = await adapter.verify_success(page)

            # STEP 12: Capture screenshot (always, regardless of success)
            screenshot_path = await capture_and_store_screenshot(page, package.application_id)

            # STEP 13: Transition to SUBMITTED if verified
            if verified:
                status = "SUBMITTED"
                await transition_status(package.application_id, "SUBMITTED", {
                    "screenshot_url": screenshot_path,
                    "confirmation_text": confirmation_text
                })
            else:
                status = "FORM_COMPLETED" # If not verified, but submitted, consider it form completed
                await transition_status(package.application_id, "FORM_COMPLETED")

            # STEP 14: Save session
            if context_mgr and context:
                await context_mgr.save_session(package.candidate_id, package.platform, context)
                await context_mgr.destroy_context(context)

            # STEP 15: Return result
            return ApplicationResult(
                application_id=package.application_id,
                status=status,
                screenshot_url=screenshot_path,
                confirmation_text=confirmation_text,
                execution_time_seconds=time.time() - start_time,
                retry_count=retry_count
            )

        except Exception as e:
            error_message = str(e)
            logger.error(f"Application {package.application_id} encountered an error: {e}")

            if "BLOCKED" in error_message: # Simple check for BLOCKED scenario
                status = "BLOCKED"
                # Removed transition_status("BLOCKED") - rely on events
            elif retry_count < 3:
                # Re-raise to let Celery handle retry
                raise
            else:
                status = "FAILED"
                # Removed transition_status("FAILED") - rely on events
            
            if context and page and context.is_connected(): # Check if context/page is still alive before screenshot
                screenshot_path = await capture_and_store_screenshot(page, package.application_id)

            # Clean up browser context in case of error
            if context_mgr and context:
                try:
                    await context_mgr.destroy_context(context)
                except Exception as close_e:
                    logger.error(f"Error destroying browser context for {package.application_id}: {close_e}")

            return ApplicationResult(
                application_id=package.application_id,
                status=status,
                screenshot_url=screenshot_path,
                confirmation_text=confirmation_text,
                error_message=error_message,
                execution_time_seconds=time.time() - start_time,
                retry_count=retry_count
            )
