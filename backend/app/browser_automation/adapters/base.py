import asyncio
import os
import random
from abc import ABC, abstractmethod
from playwright.async_api import Page
from typing import Optional, Tuple

class BasePlatformAdapter(ABC):
    platform_name: str
    container_selector: Optional[str] = None

    def set_candidate_credentials(self, credentials: Optional[dict]) -> None:
        """Inject the candidate's portal login credentials (from their DB
        profile) so login-gated adapters authenticate AS that candidate.

        Deliberately a separate channel from ``candidate_profile`` — the
        password must never reach LLM prompts or logs. Adapters that don't log
        in simply never read it. Shape: ``{"login_email","password","gmail"}``.
        """
        self._candidate_credentials = dict(credentials or {})

    def _spawn_delegate(self, key_or_url):
        """get_adapter() + credential propagation. Aggregator adapters that
        resolve to an inner ATS adapter MUST create it via this helper — a bare
        get_adapter() drops the candidate's login credentials, so login-gated
        delegates (Workday, iCIMS, Dice) raise LOGIN_REQUIRED even when the
        candidate has portal credentials on file."""
        from .registry import get_adapter
        inner = get_adapter(key_or_url)
        creds = getattr(self, "_candidate_credentials", None)
        if creds:
            try:
                inner.set_candidate_credentials(creds)
            except Exception:
                pass
        return inner

    def _login_credential(self, field: str, *env_fallback_keys: str) -> str:
        """Resolve a login credential, preferring the injected per-candidate
        value and falling back to environment variables (first non-empty).

        `field` is one of ``"login_email"`` / ``"password"`` / ``"gmail"``.
        Per-candidate credentials win so multi-candidate runs each log in as
        themselves; env vars remain the fallback for shared/test accounts.
        """
        creds = getattr(self, "_candidate_credentials", None) or {}
        val = (creds.get(field) or "").strip()
        if val:
            return val
        for key in env_fallback_keys:
            v = os.getenv(key, "").strip()
            if v:
                return v
        return ""

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

    async def refresh_frame(self, page: Page) -> None:
        pass

    async def human_delay(self, min_s=2.0, max_s=8.0):
        await asyncio.sleep(random.uniform(min_s, max_s))
