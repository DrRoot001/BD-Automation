import asyncio
import random
from abc import ABC, abstractmethod
from playwright.async_api import Page
from typing import Optional, Tuple

class BasePlatformAdapter(ABC):
    platform_name: str
    container_selector: Optional[str] = None

    @abstractmethod
    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        pass

    @abstractmethod
    async def detect_application_type(self, page: Page) -> str:
        pass

    @abstractmethod
    async def fill_application(self, page: Page, profile: dict, resume_path: str, cover_letter_path: Optional[str], screening_answers: Optional[dict], pre_detected_form=None, candidate_id: Optional[str] = None) -> bool:
        pass

    @abstractmethod
    async def submit(self, page: Page) -> bool:
        pass

    @abstractmethod
    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        pass

    async def human_delay(self, min_s=2.0, max_s=8.0):
        await asyncio.sleep(random.uniform(min_s, max_s))
