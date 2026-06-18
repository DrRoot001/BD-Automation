"""URL canonicalization for deduplication.

Normalizes URLs by stripping tracking parameters and path variations
to enable accurate duplicate detection across platforms.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


def canonicalize_url(url: str) -> str:
    """Normalize URL: strip tracking params, lowercase, remove fragments.
    
    This is used in Submodule 5 for Layer 1 deduplication.
    
    Args:
        url: Raw job URL.
    
    Returns:
        Canonical URL suitable for dedup comparison.
    
    Examples:
        "https://example.com/jobs/123?utm_source=linkedin&utm_medium=social"
        → "https://example.com/jobs/123"
        
        "https://EXAMPLE.COM/jobs/123/"
        → "https://example.com/jobs/123"
    """
    if not url:
        return ""
    
    try:
        parsed = urlparse(url)
        
        # Lowercase scheme and netloc
        scheme = parsed.scheme.lower() if parsed.scheme else "https"
        netloc = parsed.netloc.lower()
        
        # Normalize path (lowercase, remove trailing slash)
        path = parsed.path.lower().rstrip("/") if parsed.path else ""
        
        # Remove known tracking parameters
        tracking_params = {
            "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
            "ref", "source", "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid",
        }
        
        # Parse query parameters
        query_params = parse_qs(parsed.query, keep_blank_values=False)
        
        # Filter out tracking params
        cleaned_params = {}
        for key, values in query_params.items():
            if key.lower() not in tracking_params:
                # Keep only the first value for simplicity
                cleaned_params[key] = values[0] if values else ""
        
        # Rebuild query string (sorted for consistency)
        if cleaned_params:
            new_query = urlencode(sorted(cleaned_params.items()))
        else:
            new_query = ""
        
        # Reconstruct URL (no fragment)
        canonical = urlunparse((
            scheme,
            netloc,
            path,
            "",  # params (rarely used)
            new_query,
            ""  # fragment
        ))
        
        return canonical
    
    except Exception as e:
        # If parsing fails, return lowercased URL as fallback
        print(f"Error canonicalizing URL {url}: {e}")
        return url.lower()


def url_hash(url: str) -> str:
    """Generate a hash of the canonical URL for fast lookup.
    
    Used for Redis caching in Layer 1 dedup.
    
    Args:
        url: Raw job URL.
    
    Returns:
        SHA256 hex digest of canonical URL.
    """
    import hashlib
    canonical = canonicalize_url(url)
    return hashlib.sha256(canonical.encode()).hexdigest()


def are_urls_equivalent(url1: str, url2: str) -> bool:
    """Check if two URLs are equivalent (same canonical form).
    
    Args:
        url1: First URL.
        url2: Second URL.
    
    Returns:
        True if URLs canonicalize to the same form.
    """
    return canonicalize_url(url1) == canonicalize_url(url2)
