"""Adapter registry and registration decorator.

All adapters auto-register when imported. This enables dynamic adapter
discovery and instantiation at runtime.
"""

from __future__ import annotations

from typing import Dict, Optional, Type

from .base import BaseSourceAdapter

# Global registry of all adapters
ADAPTER_REGISTRY: Dict[str, Type[BaseSourceAdapter]] = {}


def register_adapter(cls: Type[BaseSourceAdapter]) -> Type[BaseSourceAdapter]:
    """Decorator to register an adapter in the global registry.
    
    Usage:
        @register_adapter
        class MyAdapter(BaseSourceAdapter):
            platform_name = "my_platform"
            ...
    
    Args:
        cls: Adapter class inheriting from BaseSourceAdapter.
    
    Returns:
        The same class (decorator pattern).
    
    Raises:
        ValueError: If platform_name is not set.
    """
    name = getattr(cls, "platform_name", None)
    if not name:
        raise ValueError(f"Adapter {cls.__name__} must define platform_name")
    
    ADAPTER_REGISTRY[name] = cls
    print(f"✓ Registered adapter: {name} ({cls.__name__})")
    return cls


def get_adapter(name: str) -> Optional[Type[BaseSourceAdapter]]:
    """Retrieve an adapter class by platform name.
    
    Args:
        name: Platform name (e.g., "greenhouse", "lever", "rss_generic").
    
    Returns:
        Adapter class if found, None otherwise.
    """
    return ADAPTER_REGISTRY.get(name)


def list_adapters() -> Dict[str, Type[BaseSourceAdapter]]:
    """Return all registered adapters.
    
    Returns:
        Dictionary of {platform_name: AdapterClass}.
    """
    return ADAPTER_REGISTRY.copy()


def is_adapter_registered(name: str) -> bool:
    """Check if an adapter is registered.
    
    Args:
        name: Platform name.
    
    Returns:
        True if adapter is registered, False otherwise.
    """
    return name in ADAPTER_REGISTRY
