"""Adapter registry compatibility package for module2."""

from typing import Any


class BaseSourceAdapter:
    platform_name: str = "base"
    ingestion_type: str = "api"

    async def discover_jobs(self, filters: Any | None = None) -> list[dict[str, Any]]:
        return []

    async def get_job_detail(self, job_url: str) -> dict[str, Any]:
        return {"url": job_url}


ADAPTER_REGISTRY: dict[str, type[BaseSourceAdapter]] = {}


def register_adapter(cls: type[BaseSourceAdapter]) -> type[BaseSourceAdapter]:
    ADAPTER_REGISTRY[cls.platform_name] = cls
    return cls


def get_adapter(platform_name: str) -> type[BaseSourceAdapter] | None:
    return ADAPTER_REGISTRY.get(platform_name)
