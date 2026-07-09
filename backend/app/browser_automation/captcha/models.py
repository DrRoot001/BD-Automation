from pydantic import BaseModel
from typing import Literal, Optional

class CaptchaSolution(BaseModel):
    captcha_type: Literal["recaptcha_v2", "recaptcha_v3", "recaptcha_invisible", "hcaptcha", "turnstile", "image", "slider"]
    token: Optional[str] = None
    success: bool
    solve_time_seconds: float
    cost_usd: float
    # Optional short failure reason. When a captcha genuinely cannot be solved
    # with the available providers/IP, this is set to a string prefixed EXACTLY
    # with "CAPTCHA_UNSUPPORTED:" so the executor can map it to a clean terminal
    # BLOCKED (rather than retrying or crashing). Normal/transient failures leave
    # this None so existing retry logic is unaffected.
    error: Optional[str] = None
