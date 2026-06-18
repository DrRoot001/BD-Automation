"""Base adapter interface for Module 2 source connectors.

Every platform connector (Greenhouse, Lever, Indeed, LinkedIn, RSS, etc.)
must inherit from BaseSourceAdapter and implement the interface.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional


@dataclass
class RawJobData:
    """Raw job data as scraped/fetched from a source, before normalization.
    
    This is the output of Submodule 1 (Source Adapters) and input to
    Submodule 2 (Normalization Engine).
    """
    
    title: str
    company: str
    location: Optional[str] = None
    url: str = ""
    description: Optional[str] = None
    salary_text: Optional[str] = None  # raw salary string, e.g., "$120k - $180k"
    posted_at: Optional[datetime] = None
    raw_html: Optional[str] = None  # for scraper sources
    raw_json: Optional[Dict[str, Any]] = None  # for API sources
    source_platform: str = "unknown"
    
    def __post_init__(self):
        """Validate required fields."""
        if not self.title or not self.url:
            raise ValueError("RawJobData requires title and url")


@dataclass
class RateLimitConfig:
    """Rate limiting configuration for a platform adapter."""
    
    max_per_hour: int = 1000
    max_per_day: int = 10000
    delay_between_requests_seconds: tuple[float, float] = (0.5, 1.5)  # (min, max)


class BaseSourceAdapter(ABC):
    """Abstract base class for all job source adapters.
    
    Subclasses must implement discover_jobs() and get_job_detail().
    
    Example usage:
        adapter = GreenhouseAdapter(config={"api_key": "..."})
        jobs = await adapter.discover_jobs(filters={
            "company": "acme-corp",
            "location": "USA",
            "job_types": ["contract", "part-time"],
        })
    """
    
    platform_name: str = "base"
    ingestion_type: Literal["api", "scraper", "rss", "enterprise_ats"] = "api"
    
    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Initialize adapter with platform-specific configuration.
        
        Args:
            config: Dictionary with keys like api_key, auth_token, base_url, etc.
        """
        self.config = config or {}
        self.session = None  # Will be set up for HTTP adapters
    
    @abstractmethod
    async def discover_jobs(self, filters: Dict[str, Any]) -> List[RawJobData]:
        """Discover jobs matching the given filters.
        
        Args:
            filters: Dictionary with standard and platform-specific filter keys:
                - location: str (default "USA")
                - remote_only: bool (default True)
                - job_types: list[str] (default ["full-time", "contract"])
                - min_hourly_rate: int (default 60)
                - min_annual_salary: int (default 120000)
                - required_skills: list[str] (default [])
                - exclude_keywords: list[str] (default [])
                - posted_within_days: int (default 7)
                - Platform-specific: company, keywords, rss_url, etc.
        
        Returns:
            List of RawJobData objects discovered from the platform.
        
        Raises:
            Exception: Network errors, API errors, etc.
        """
        ...
    
    @abstractmethod
    async def get_job_detail(self, job_url: str) -> RawJobData:
        """Fetch full details for a single job listing.
        
        Args:
            job_url: Original job URL from the platform.
        
        Returns:
            Complete RawJobData for the job.
        """
        ...
    
    def get_rate_limit_config(self) -> RateLimitConfig:
        """Return platform-specific rate limits.
        
        Override in subclasses if custom limits are needed.
        """
        return RateLimitConfig()
    
    async def close(self) -> None:
        """Clean up resources (connections, sessions, etc.).
        
        Override in subclasses if needed.
        """
        pass
    
    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(platform={self.platform_name}, type={self.ingestion_type})"
