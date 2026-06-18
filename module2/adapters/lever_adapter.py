"""Lever ATS adapter for job discovery.

Lever provides a public API for company job postings.
API: https://api.lever.co/v0/postings/{company}
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from .base import BaseSourceAdapter, RawJobData
from .registry import register_adapter


@register_adapter
class LeverAdapter(BaseSourceAdapter):
    """Lever ATS public API adapter.
    
    Discovers jobs from company Lever job boards.
    
    Example:
        adapter = LeverAdapter()
        jobs = await adapter.discover_jobs({"company": "figma"})
    """
    
    platform_name = "lever"
    ingestion_type = "api"
    base_url = "https://api.lever.co/v0/postings"
    
    async def discover_jobs(self, filters: Dict[str, Any]) -> List[RawJobData]:
        """Discover jobs from a Lever board.
        
        Args:
            filters: Must contain 'company' key (e.g., "figma", "twilio").
                    Optional: 'location', 'remote_only', 'job_types'.
        
        Returns:
            List of RawJobData objects from the Lever board.
        """
        company = filters.get("company")
        if not company:
            raise ValueError("Lever adapter requires 'company' in filters")
        
        url = f"{self.base_url}/{company}?mode=json"
        loop = asyncio.get_event_loop()
        
        try:
            payload = await loop.run_in_executor(None, self._fetch_json, url)
        except Exception as e:
            print(f"Error fetching Lever jobs for {company}: {e}")
            return []
        
        if not isinstance(payload, list):
            return []
        
        items = []
        for job_data in payload:
            title = job_data.get("text") or job_data.get("title", "")
            title = title.strip()
            
            # Extract location from categories
            location = None
            if job_data.get("categories"):
                location = job_data["categories"].get("location")
            
            # Extract job URL
            job_url = job_data.get("applyUrl") or ""
            if not job_url and job_data.get("internals"):
                job_url = job_data["internals"].get("hostedUrl", "")
            
            description = job_data.get("description", "").strip()
            posted_at = self._parse_date(job_data.get("createdAt"))
            
            if not title or not job_url:
                continue
            
            items.append(RawJobData(
                title=title,
                company=company,
                location=location or "Unknown",
                url=job_url,
                description=description,
                posted_at=posted_at,
                raw_json=job_data,
                source_platform=self.platform_name,
            ))
        
        return items
    
    async def get_job_detail(self, job_url: str) -> RawJobData:
        """Fetch full details for a single job."""
        return RawJobData(
            title="",
            company="",
            location=None,
            url=job_url,
            description=None,
            source_platform=self.platform_name,
        )
    
    @staticmethod
    def _fetch_json(url: str) -> Any:
        """Fetch and parse JSON from URL."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        req = Request(url, headers=headers)
        
        try:
            with urlopen(req, timeout=10) as response:
                content = response.read().decode("utf-8")
                return json.loads(content)
        except (URLError, HTTPError) as e:
            print(f"HTTP Error: {e}")
            return None
    
    @staticmethod
    def _parse_date(timestamp_ms: int | None) -> datetime | None:
        """Parse timestamp (milliseconds since epoch) from Lever."""
        if not timestamp_ms:
            return None
        
        try:
            # Lever uses milliseconds since epoch
            return datetime.fromtimestamp(timestamp_ms / 1000.0)
        except (ValueError, TypeError, OSError):
            return None
