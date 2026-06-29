import hashlib
import random
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
    # Per-candidate DETERMINISTIC fingerprint. A given candidate must present the
    # same viewport / timezone / locale / UA on every run, otherwise an ATS that
    # fingerprints across sessions sees one "person" whose device keeps changing
    # — a strong bot signal. We seed a local RNG from a hash of candidate_id so
    # the choice is stable per candidate but still varies between candidates.
    seed = int(hashlib.sha256((candidate_id or "default").encode("utf-8")).hexdigest(), 16)
    rng = random.Random(seed)
    viewport = dict(rng.choice(VIEWPORTS))
    timezone = rng.choice(TIMEZONES)
    locale = rng.choice(["en-US", "en-GB", "en-CA", "en-AU"])
    user_agent = rng.choice(USER_AGENTS)

    return StealthConfig(
        viewport=viewport,
        user_agent=user_agent,
        timezone=timezone,
        locale=locale,
        webgl_vendor="Intel Inc.",
        canvas_noise=True,
        webdriver_patch=True
    )
