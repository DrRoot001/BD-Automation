import logging
from typing import Optional, Tuple
from playwright.async_api import Page
from .base import BasePlatformAdapter
from ..forms import detect_form, fill_form, upload_file

logger = logging.getLogger(__name__)

class GreenhouseAdapter(BasePlatformAdapter):
    platform_name = "greenhouse"
    # boards.greenhouse.io (hosted Boards v2) has no #application_form wrapper;
    # the old embedded widget used that id. None = whole-page scan.
    container_selector = None

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        try:
            await page.goto(job_url, wait_until="networkidle", timeout=10000)
        except Exception:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=20000)
        await self.human_delay()

    async def detect_application_type(self, page: Page) -> str:
        return "EXTERNAL_FORM"

    async def fill_application(self, page: Page, profile: dict, resume_path: str, cover_letter_path: Optional[str], screening_answers: Optional[dict]) -> bool:
        form = await detect_form(page, container_selector=self.container_selector)

        # Log all detected file fields for debugging
        file_fields = [f for f in form.fields if f.field_type == "file"]
        logger.info(f"[GH] Detected {len(file_fields)} file field(s): "
                    f"{[(f.label, f.selector) for f in file_fields]}")

        # Fill basic fields
        fill_success = await fill_form(page, form, profile, screening_answers)

        # ── Greenhouse Boards file upload ──
        # boards.greenhouse.io uses hidden <input name="file-attachment"> elements
        # (display:none, no id). The label falls back to the name attribute, so
        # label-matching for "resume" never fires. We target by DOM order instead:
        # first file-attachment = resume, second = cover letter (Greenhouse convention).
        await self._upload_greenhouse_files(page, resume_path, cover_letter_path)

        return fill_success

    async def _upload_greenhouse_files(self, page: Page, resume_path: Optional[str], cover_letter_path: Optional[str]) -> None:
        # Primary: Greenhouse Boards v2 hidden file inputs (name='file-attachment')
        gh_inputs = page.locator("input[name='file-attachment']")
        count = await gh_inputs.count()
        logger.info(f"[GH] Found {count} input[name='file-attachment'] element(s)")

        if count > 0 and resume_path:
            try:
                await gh_inputs.nth(0).set_input_files(resume_path)
                logger.info(f"[GH] Resume uploaded to input[name='file-attachment'][0]: {resume_path}")
            except Exception as e:
                logger.error(f"[GH] Resume upload failed on file-attachment[0]: {e}")

        if count > 1 and cover_letter_path:
            try:
                await gh_inputs.nth(1).set_input_files(cover_letter_path)
                logger.info(f"[GH] Cover letter uploaded to input[name='file-attachment'][1]: {cover_letter_path}")
            except Exception as e:
                logger.error(f"[GH] Cover letter upload failed on file-attachment[1]: {e}")

        # Fallback: generic input[type='file'] for older embedded Greenhouse widgets
        if count == 0:
            logger.warning("[GH] No file-attachment inputs found; trying generic file input fallback")
            generic = page.locator("input[type='file']")
            gen_count = await generic.count()
            if gen_count > 0 and resume_path:
                try:
                    await generic.first.set_input_files(resume_path)
                    logger.info(f"[GH] Resume uploaded via generic fallback: {resume_path}")
                except Exception as e:
                    logger.error(f"[GH] Generic file upload fallback failed: {e}")

    async def submit(self, page: Page) -> bool:
        submit_btn = await page.query_selector("button[type=submit], #submit_app")
        if submit_btn:
            await submit_btn.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            await self.human_delay()
            return True
        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        content = (await page.content()).lower()
        success_patterns = ["application has been submitted", "thank you", "successfully applied"]
        for pattern in success_patterns:
            if pattern in content:
                return True, pattern
        return False, None
