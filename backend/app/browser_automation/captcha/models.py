from pydantic import BaseModel
from typing import Literal, Optional

class CaptchaSolution(BaseModel):
    captcha_type: Literal["recaptcha_v2", "recaptcha_v3", "hcaptcha", "image", "slider"]
    token: Optional[str] = None
    success: bool
    solve_time_seconds: float
    cost_usd: float
