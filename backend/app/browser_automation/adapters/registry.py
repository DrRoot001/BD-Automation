from .base import BasePlatformAdapter
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter
from .ashby import AshbyAdapter
from .generic import GenericFormAdapter
from .linkedin import LinkedInEasyApplyAdapter

ADAPTER_REGISTRY: dict[str, type[BasePlatformAdapter]] = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
    "linkedin": LinkedInEasyApplyAdapter,
    "generic": GenericFormAdapter,
}

def get_adapter(platform: str) -> BasePlatformAdapter:
    cls = ADAPTER_REGISTRY.get(platform.lower(), GenericFormAdapter)
    return cls()
