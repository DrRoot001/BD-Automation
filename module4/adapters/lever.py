from typing import Optional, Tuple
from playwright.async_api import Page
from .base import BasePlatformAdapter
from ..forms import detect_form, fill_form, upload_file

class LeverAdapter(BasePlatformAdapter):
    platform_name = "lever"
    container_selector = "#application-form"

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        await page.goto(job_url, wait_until="networkidle")
        await self.human_delay()

    async def detect_application_type(self, page: Page) -> str:
        return "EXTERNAL_FORM"

    async def fill_application(self, page: Page, profile: dict, resume_path: str, cover_letter_path: Optional[str], screening_answers: Optional[dict]) -> bool:
        form = await detect_form(page, container_selector=self.container_selector)
        fill_success = await fill_form(page, form, profile, screening_answers)
        
        for field in form.fields:
            if field.field_type == "file" and "resume" in field.label.lower():
                await upload_file(page, field.selector, resume_path)
            if cover_letter_path and field.field_type == "file" and "cover letter" in field.label.lower():
                await upload_file(page, field.selector, cover_letter_path)
                
        return fill_success

    async def submit(self, page: Page) -> bool:
        submit_btn = await page.query_selector(".template-btn-submit, button[data-qa=btn-submit]")
        if submit_btn:
            await submit_btn.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            await self.human_delay()
            return True
        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        content = (await page.content()).lower()
        success_patterns = ["thanks for applying", "application submitted"]
        for pattern in success_patterns:
            if pattern in content:
                return True, pattern
        return False, None
