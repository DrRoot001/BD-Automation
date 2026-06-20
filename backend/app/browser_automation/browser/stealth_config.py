import hashlib
from pydantic import BaseModel
from typing import Dict, List

USER_AGENTS = [
    # Current Chrome 136 (most common as of mid-2026)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864}
]

TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles"
]

class StealthConfig(BaseModel):
    viewport: Dict[str, int]
    user_agent: str
    timezone: str
    locale: str
    webgl_vendor: str
    canvas_noise: bool
    webdriver_patch: bool

def get_stealth_config(candidate_id: str) -> StealthConfig:
    seed_hex = hashlib.sha256(candidate_id.encode()).hexdigest()
    seed = int(seed_hex, 16)
    
    viewport = VIEWPORTS[seed % len(VIEWPORTS)]
    user_agent = USER_AGENTS[seed % len(USER_AGENTS)]
    timezone = TIMEZONES[seed % len(TIMEZONES)]
    
    return StealthConfig(
        viewport=viewport,
        user_agent=user_agent,
        timezone=timezone,
        locale="en-US",
        webgl_vendor="Intel Inc.",
        canvas_noise=True,
        webdriver_patch=True
    )
