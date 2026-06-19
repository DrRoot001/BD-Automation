from typing import Optional, Tuple
from playwright.async_api import Page
from .base import BasePlatformAdapter
from ..forms import detect_form, fill_form, upload_file

class GenericFormAdapter(BasePlatformAdapter):
    platform_name = "generic"

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        await page.goto(job_url, wait_until="domcontentloaded")
        await self.human_delay()

    async def detect_application_type(self, page: Page) -> str:
        form = await detect_form(page, container_selector=self.container_selector)
        return form.form_type

    async def fill_application(self, page: Page, profile: dict, resume_path: str, cover_letter_path: Optional[str], screening_answers: Optional[dict]) -> bool:
        form = await detect_form(page, container_selector=self.container_selector)
        fill_success = await fill_form(page, form, profile, screening_answers)
        
        for field in form.fields:
            if field.field_type == "file":
                if "resume" in field.label.lower() or "cv" in field.label.lower():
                    await upload_file(page, field.selector, resume_path)
                elif cover_letter_path and "cover letter" in field.label.lower():
                    await upload_file(page, field.selector, cover_letter_path)
                
        return fill_success

    async def submit(self, page: Page) -> bool:
        selectors = [
            "button[type=submit]", 
            "input[type=submit]", 
            "button:has-text('Submit')", 
            "button:has-text('Apply')", 
            "button:has-text('Send Application')"
        ]
        for selector in selectors:
            btn = await page.query_selector(selector)
            if btn:
                await btn.click()
                await self.human_delay()
                return True
        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        content = (await page.content()).lower()
        success_patterns = [
            "thank you", "application received", "successfully submitted", 
            "we'll be in touch", "application complete"
        ]
        for pattern in success_patterns:
            if pattern in content:
                return True, pattern
        return False, None
