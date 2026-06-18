"""Indeed adapter (RSS-based) for job discovery.

This adapter uses Indeed's RSS search feed when available. It supports two modes:
- Provide `rss_url` in `filters` to use a specific feed URL.
- Provide `query` (and optional `location`) to build a search RSS URL.

Note: Indeed pages may block scraping; using the RSS feed is the lightweight option.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET
import email.utils

from .base import BaseSourceAdapter, RawJobData
from .registry import register_adapter


@register_adapter
class IndeedAdapter(BaseSourceAdapter):
    """Indeed adapter using public RSS search feeds when available.

    Usage:
        adapter = IndeedAdapter()
        jobs = await adapter.discover_jobs({"query": "software engineer", "location": "USA"})
        # or
        jobs = await adapter.discover_jobs({"rss_url": "https://www.indeed.com/rss?q=python"})
    """

    platform_name = "indeed"
    ingestion_type = "rss"

    DEFAULT_RSS_BASE = "https://www.indeed.com/rss"

    async def discover_jobs(self, filters: Dict[str, Any]) -> List[RawJobData]:
        rss_url = filters.get("rss_url")
        query = filters.get("query")
        location = filters.get("location")
        company = filters.get("company") or "indeed"

        if not rss_url:
            if not query:
                raise ValueError("Indeed adapter requires 'query' or 'rss_url' in filters")
            # Build a simple Indeed RSS search URL
            q = quote_plus(str(query))
            rss_url = f"{self.DEFAULT_RSS_BASE}?q={q}"
            if location:
                rss_url += f"&l={quote_plus(str(location))}"

        try:
            content = await self._fetch_text(rss_url)
        except Exception as e:
            print(f"Error fetching Indeed RSS: {e}")
            return []

        if not content:
            return []

        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return []

        items = []
        # RSS structure: <rss><channel><item>...
        channel = root.find("channel") or root
        for item in channel.findall("item"):
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")
            pub_el = item.find("pubDate")

            title = (title_el.text or "").strip() if title_el is not None else ""
            link = (link_el.text or "").strip() if link_el is not None else ""
            description = (desc_el.text or "").strip() if desc_el is not None else None

            posted_at = None
            if pub_el is not None and pub_el.text:
                try:
                    posted_at = email.utils.parsedate_to_datetime(pub_el.text)
                except Exception:
                    posted_at = None

            if not title or not link:
                continue

            items.append(RawJobData(
                title=title,
                company=company,
                location=location or None,
                url=link,
                description=description,
                posted_at=posted_at,
                raw_html=None,
                raw_json=None,
                source_platform=self.platform_name,
            ))

        return items

    async def get_job_detail(self, job_url: str) -> RawJobData:
        # For RSS-based jobs, the RSS item usually contains sufficient detail.
        return RawJobData(
            title="",
            company="",
            location=None,
            url=job_url,
            description=None,
            source_platform=self.platform_name,
        )

    async def _fetch_text(self, url: str) -> Optional[str]:
        loop = __import__("asyncio").get_event_loop()

        def _fetch():
            headers = {"User-Agent": "Mozilla/5.0 (compatible)"}
            req = Request(url, headers=headers)
            try:
                with urlopen(req, timeout=15) as resp:
                    return resp.read().decode("utf-8")
            except (URLError, HTTPError) as e:
                print(f"HTTP Error fetching RSS: {e}")
                return None

        return await loop.run_in_executor(None, _fetch)
