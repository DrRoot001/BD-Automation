"""RSS feed adapter for job discovery.

Supports standard RSS feeds from job boards like We Work Remotely, Remote OK, Remotive.
Uses stdlib xml.etree for lightweight parsing (no external dependencies).
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List
from urllib.request import urlopen

from .base import BaseSourceAdapter, RawJobData
from .registry import register_adapter


@register_adapter
class RssAdapter(BaseSourceAdapter):
    """Generic RSS feed adapter for job discovery.
    
    Platforms supported:
    - We Work Remotely: https://weworkremotely.com/categories/remote-jobs/jobs.rss
    - Remote OK: https://remoteok.com/feed
    - Remotive: https://remotive.com/remote-jobs/rss
    """
    
    platform_name = "rss_generic"
    ingestion_type = "rss"
    
    async def discover_jobs(self, filters: Dict[str, Any]) -> List[RawJobData]:
        """Discover jobs from an RSS feed.
        
        Args:
            filters: Must contain 'rss_url' key with the feed URL.
                    Optional: 'location', 'remote_only', 'job_types', 'required_skills'.
        
        Returns:
            List of RawJobData objects from the feed.
        """
        rss_url = filters.get("rss_url")
        if not rss_url:
            raise ValueError("RSS adapter requires 'rss_url' in filters")
        
        location_filter = filters.get("location", "").lower()
        remote_only = filters.get("remote_only", True)
        
        request_headers = filters.get("request_headers")
        loop = asyncio.get_event_loop()
        try:
            content = await loop.run_in_executor(None, self._fetch_feed, rss_url, request_headers)
        except Exception as e:
            print(f"Error fetching RSS feed {rss_url}: {e}")
            return []
        
        try:
            root = ET.fromstring(content)
        except ET.ParseError as e:
            print(f"Error parsing RSS feed: {e}")
            return []
        
        items = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            description = item.findtext("description", "").strip()
            pubdate_text = item.findtext("pubDate", "")
            
            if not title or not link:
                continue
            
            # Parse publication date
            posted_at = self._parse_date(pubdate_text)
            
            # Basic filtering
            if remote_only and "remote" not in description.lower() and "remote" not in title.lower():
                continue
            
            items.append(RawJobData(
                title=title,
                company="Unknown",
                location="Remote" if remote_only else None,
                url=link,
                description=description,
                posted_at=posted_at,
                source_platform=self.platform_name,
            ))
        
        return items
    
    async def get_job_detail(self, job_url: str) -> RawJobData:
        """Fetch details for a single job.
        
        For RSS feeds, details are often already in the feed, so this
        is a minimal implementation.
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
    def _fetch_feed(url: str, request_headers: Dict[str, str] = None) -> bytes:
        """Fetch RSS feed content."""
        from urllib.request import Request
        import ssl
        try:
            context = ssl._create_unverified_context()
        except AttributeError:
            context = None
            
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0)",
            "Accept": "application/rss+xml, application/xml, text/xml"
        }
        if request_headers:
            headers.update(request_headers)
            
        req = Request(url, headers=headers)
        with urlopen(req, timeout=10, context=context) as response:
            return response.read()
    
    @staticmethod
    def _parse_date(pubdate_text: str) -> datetime | None:
        """Parse RFC 2822 format date from RSS (e.g., 'Mon, 18 Jun 2026 10:30:00 +0000')."""
        if not pubdate_text:
            return None
        
        formats = [
            "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S +0000",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(pubdate_text.strip(), fmt)
            except ValueError:
                continue
        
        return None
