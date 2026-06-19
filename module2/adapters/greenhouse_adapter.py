"""Greenhouse ATS adapter for job discovery.

Greenhouse provides a public JSON feed for company job boards.
API: https://boards-api.greenhouse.io/v1/boards/{company}/jobs

NOTE: Only US-based jobs (including Remote) are returned.
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
from module2.normalization.helpers import is_usa_location


@register_adapter
class GreenhouseAdapter(BaseSourceAdapter):
    """Greenhouse ATS public API adapter.
    
    Discovers jobs from company Greenhouse job boards.
    
    Example:
        adapter = GreenhouseAdapter()
        jobs = await adapter.discover_jobs({"company": "stripe"})
    """
    
    platform_name = "greenhouse"
    ingestion_type = "api"
    base_url = "https://boards-api.greenhouse.io/v1/boards"
    
    async def discover_jobs(self, filters: Dict[str, Any]) -> List[RawJobData]:
        """Discover jobs from a Greenhouse board.
        
        Args:
            filters: Must contain 'company' key (e.g., "stripe", "github").
                    Optional: 'location', 'remote_only', 'job_types'.
        
        Returns:
            List of RawJobData objects from the Greenhouse board.
        """
        company = filters.get("company")
        if not company:
            raise ValueError("Greenhouse adapter requires 'company' in filters")
        
        url = f"{self.base_url}/{company}/jobs"
        loop = asyncio.get_event_loop()
        
        try:
            payload = await loop.run_in_executor(None, self._fetch_json, url)
        except Exception as e:
            print(f"Error fetching Greenhouse jobs for {company}: {e}")
            return []
        
        if not payload or "jobs" not in payload:
            return []
        
        items = []
        skipped_non_usa = 0
        for job_data in payload.get("jobs", []):
            title = job_data.get("title", "").strip()
            location_obj = job_data.get("location", {})
            location = location_obj.get("name") if location_obj else None
            job_url = job_data.get("absolute_url") or job_data.get("url") or ""
            description = job_data.get("content", "").strip()
            posted_at = self._parse_date(job_data.get("created_at"))
            
            if not title or not job_url:
                continue
            
            # ── USA-only enforcement ─────────────────────────────────────────
            if not is_usa_location(location or ""):
                skipped_non_usa += 1
                continue
            # ────────────────────────────────────────────────────────────────
            
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
        
        if skipped_non_usa:
            print(f"[GreenhouseAdapter] Skipped {skipped_non_usa} non-USA jobs for {company}")
        
        return items
    
    async def get_job_detail(self, job_url: str) -> RawJobData:
        """Fetch full details for a single job.
        
        Greenhouse details are often included in discover_jobs output,
        so this fetches the page URL if needed.
        """
        return RawJobData(
            title="",
            company="",
            location=None,
            url=job_url,
            description=None,
            source_platform=self.platform_name,
        )
    
    @staticmethod
    def _fetch_json(url: str) -> Dict[str, Any]:
        """Fetch and parse JSON from URL."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        req = Request(url, headers=headers)
        
        import ssl
        try:
            context = ssl._create_unverified_context()
        except AttributeError:
            context = None
            
        try:
            with urlopen(req, timeout=10, context=context) as response:
                content = response.read().decode("utf-8")
                return json.loads(content)
        except (URLError, HTTPError) as e:
            print(f"HTTP Error: {e}")
            return {}
    
    @staticmethod
    def _parse_date(date_str: str) -> datetime | None:
        """Parse ISO format date from Greenhouse."""
        if not date_str:
            return None
        
        try:
            # Greenhouse uses ISO 8601: "2026-06-18T10:30:00Z"
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
