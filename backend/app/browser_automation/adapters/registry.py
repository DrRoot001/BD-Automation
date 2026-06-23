from .ashby import AshbyAdapter
from .base import BasePlatformAdapter
from .generic import GenericFormAdapter
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter
from .linkedin import LinkedInEasyApplyAdapter
from .remoterocketship import RemoteRocketshipAdapter
from .workday import WorkdayAdapter


ADAPTER_REGISTRY: dict[str, type[BasePlatformAdapter]] = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
    "workday": WorkdayAdapter,
    "linkedin": LinkedInEasyApplyAdapter,
    "remoterocketship": RemoteRocketshipAdapter,
    "generic": GenericFormAdapter,
}


def get_adapter(platform: str) -> BasePlatformAdapter:
    key = (platform or "").lower().strip()
    # Normalize hyphens/underscores so "remote_rocketship" and "remote-rocketship"
    # also resolve. We only normalize for the substring lookup, not the
    # exact-key dict get, to keep canonical keys unambiguous.
    normalized = key.replace("-", "").replace("_", "")
    # Detect ATS from URL substring when caller only has the URL
    cls = ADAPTER_REGISTRY.get(key)
    if cls is None and key:
        if "remoterocketship" in normalized:
            cls = RemoteRocketshipAdapter
        elif "myworkdayjobs" in key:
            cls = WorkdayAdapter
        elif "lever.co" in key:
            cls = LeverAdapter
        elif "ashbyhq" in key:
            cls = AshbyAdapter
        elif "greenhouse" in key:
            cls = GreenhouseAdapter
        elif "linkedin" in key:
            cls = LinkedInEasyApplyAdapter
    return (cls or GenericFormAdapter)()
