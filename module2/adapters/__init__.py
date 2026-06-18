"""Submodule 1: Source Adapters

Adapters for discovering jobs from external platforms (APIs, RSS, scrapers).

All concrete adapters are imported here to auto-register via @register_adapter.
"""

from .base import BaseSourceAdapter, RawJobData, RateLimitConfig
from .registry import ADAPTER_REGISTRY, register_adapter, get_adapter, list_adapters, is_adapter_registered

# Import concrete adapters (triggers @register_adapter decorator)
from . import mock_adapter
from . import rss_adapter
from . import greenhouse_adapter
from . import lever_adapter
from . import indeed_adapter

__all__ = [
    "BaseSourceAdapter",
    "RawJobData",
    "RateLimitConfig",
    "ADAPTER_REGISTRY",
    "register_adapter",
    "get_adapter",
    "list_adapters",
    "is_adapter_registered",
]
