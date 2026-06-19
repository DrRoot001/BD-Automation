from typing import Optional, Tuple
from playwright.async_api import Page
from .base import BasePlatformAdapter
from ..forms import detect_form, fill_form, upload_file

class LinkedInEasyApplyAdapter(BasePlatformAdapter):
    platform_name = "linkedin"
    container_selector = ".jobs-easy-apply-modal"

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        await page.goto(job_url, wait_until="load")
        easy_apply_btn = await page.query_selector(".jobs-apply-button, [data-job-id] .artdeco-button--primary")
        if easy_apply_btn:
            await easy_apply_btn.click()
            await self.human_delay()

    async def detect_application_type(self, page: Page) -> str:
        return "EASY_APPLY"

    async def fill_application(self, page: Page, profile: dict, resume_path: str, cover_letter_path: Optional[str], screening_answers: Optional[dict]) -> bool:
        # Multi-step form handling
        while True:
            # Detect on current modal content
            modal = await page.query_selector(self.container_selector)
            if not modal:
                break
                
            form = await detect_form(page, container_selector=self.container_selector)
            await fill_form(page, form, profile, screening_answers)
            
            # Upload files if present
            for field in form.fields:
                if field.field_type == "file":
                    if "resume" in field.label.lower():
                        await upload_file(page, field.selector, resume_path)
                    elif cover_letter_path and "cover letter" in field.label.lower():
                        await upload_file(page, field.selector, cover_letter_path)

            next_btn = await page.query_selector("[aria-label='Continue to next step'], .artdeco-button--primary:has-text('Next'), .artdeco-button--primary:has-text('Review')")
            if next_btn:
                await next_btn.click()
                await self.human_delay(2, 5)
            else:
                break
                
        return True

    async def submit(self, page: Page) -> bool:
        submit_btn = await page.query_selector("[aria-label='Submit application'], button:has-text('Submit application')")
        if submit_btn:
            await submit_btn.click()
            await self.human_delay()
            return True
        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        content = await page.content()
        if "Your application was sent" in content:
            return True, "Your application was sent"
        return False, None
