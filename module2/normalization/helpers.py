"""Helper functions for text normalization and parsing."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


def normalize_text(text: str) -> str:
    """Normalize whitespace in text.
    
    Args:
        text: Input text.
    
    Returns:
        Text with extra whitespace removed.
    """
    if not text:
        return ""
    return " ".join(text.split())


def clean_title(title: str) -> str:
    """Clean job title: remove extra whitespace, normalize case.
    
    Args:
        title: Raw job title.
    
    Returns:
        Cleaned title.
    """
    title = normalize_text(title)
    # Remove common suffixes
    title = re.sub(r"\s*\(.*?\)\s*$", "", title)  # Remove (Remote) suffix
    title = re.sub(r"\s*-\s*\w+\s*$", "", title)  # Remove - Location suffix
    return title.strip()


def classify_job_type(title: str, description: str) -> str:
    """Classify job type from title and description.
    
    Args:
        title: Job title.
        description: Job description.
    
    Returns:
        One of: "full-time", "contract", "part-time", "temporary", "internship".
    """
    combined = (title + " " + description).lower()
    
    # Check for explicit keywords
    if any(keyword in combined for keyword in ["contract", "contractor", "c2c"]):
        return "contract"
    if any(keyword in combined for keyword in ["part-time", "part time", "pt"]):
        return "part-time"
    if any(keyword in combined for keyword in ["temporary", "temp", "freelance"]):
        return "temporary"
    if any(keyword in combined for keyword in ["internship", "intern", "graduate"]):
        return "internship"
    
    # Default to full-time
    return "full-time"


def normalize_location(location: str) -> str:
    """Normalize location string.
    
    Args:
        location: Raw location string (e.g., "Remote", "New York, NY", "USA").
    
    Returns:
        Normalized location.
    """
    if not location:
        return "Unknown"
    
    location = location.strip()
    
    # Normalize common variations
    location_lower = location.lower()
    if location_lower in ["remote", "work from home", "wfh", "fully remote"]:
        return "Remote"
    if location_lower in ["usa", "us", "united states", "anywhere us"]:
        return "USA"
    
    # Capitalize location: handle commas and state abbreviations
    # e.g., "new york, ny" -> "New York, NY"
    if "," in location:
        parts = location.split(",")
        result_parts = []
        for part in parts:
            part = part.strip()
            # If it's a 2-letter state code, uppercase it
            if len(part) == 2 and part.isalpha():
                result_parts.append(part.upper())
            else:
                result_parts.append(part.title())
        return ", ".join(result_parts)
    else:
        return location.title()


def canonicalize_url(url: str) -> str:
    """Strip tracking parameters and normalize URL.
    
    Args:
        url: Raw job URL.
    
    Returns:
        Canonical URL with tracking params removed.
    """
    if not url:
        return ""
    
    try:
        parsed = urlparse(url)
        
        # Known tracking parameters to remove
        tracking_params = {
            "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
            "ref", "source", "gclid", "fbclid", "msclkid",
        }
        
        # Parse query string and remove tracking params
        query_params = parse_qs(parsed.query, keep_blank_values=False)
        cleaned_params = {k: v for k, v in query_params.items() if k not in tracking_params}
        
        # Rebuild query string
        if cleaned_params:
            # Flatten lists back to single values (keep first)
            cleaned_params = {k: v[0] if isinstance(v, list) else v for k, v in cleaned_params.items()}
            new_query = urlencode(cleaned_params)
        else:
            new_query = ""
        
        # Reconstruct URL
        canonical = urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/"),  # Remove trailing slash
            parsed.params,
            new_query,
            ""  # No fragment
        ))
        
        return canonical.lower()
    
    except Exception:
        return url.lower()


def extract_required_years(text: str) -> Optional[int]:
    """Extract required years of experience from description.
    
    Args:
        text: Job description.
    
    Returns:
        Number of years or None if not found.
    """
    if not text:
        return None
    
    # Match patterns like "3+ years", "3-5 years", "at least 3 years"
    patterns = [
        r"(\d{1,2})\+?\s+years",  # "3+ years"
        r"(\d{1,2})\s*-\s*(\d{1,2})\s+years",  # "3-5 years"
        r"at least (\d{1,2}) years",  # "at least 3 years"
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            # Return the first captured number
            return int(match.group(1))
    
    return None


def tokenize(text: str) -> set[str]:
    """Tokenize text into lowercase words.
    
    Args:
        text: Input text.
    
    Returns:
        Set of lowercase word tokens (2+ chars).
    """
    if not text:
        return set()
    
    words = re.findall(r"\b[a-z0-9]{2,}\b", text.lower())
    return set(words)


# ─── US State & Territory Abbreviations ───────────────────────────────────────
_US_STATE_ABBREVS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    # Territories
    "DC", "PR", "GU", "VI", "AS", "MP",
}

_US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    # DC + territories
    "district of columbia", "washington d.c.", "washington dc",
    "puerto rico",
}

_US_KEYWORDS = {
    "usa", "u.s.a.", "united states", "u.s.", "us ", "remote",
    "anywhere in the us", "anywhere us", "work from home", "wfh",
    "remote-friendly",
}


def is_usa_location(location: str) -> bool:
    """Return True if the location string indicates a US-based job.

    Handles:
    - State abbreviations appended after a comma (e.g., "New York, NY")
    - State full names anywhere in the string
    - Remote / WFH positions (treated as US-eligible by default)
    - Explicit "USA" / "United States" keywords
    - Multi-location strings joined by ";" or "|"
    """
    if not location:
        return False

    loc_lower = location.lower().strip()

    # Direct US keyword match
    for kw in _US_KEYWORDS:
        if kw in loc_lower:
            return True

    # Full state name anywhere in string
    for state in _US_STATE_NAMES:
        if state in loc_lower:
            return True

    # State abbreviation after comma — e.g., "New York, NY" or "Chicago, IL"
    # Also handle multi-location strings like "New York, Ny; San Francisco, Ca"
    # Split on common separators first
    for segment in re.split(r"[;|]", location):
        segment = segment.strip()
        if "," in segment:
            parts = [p.strip() for p in segment.split(",")]
            for part in parts:
                # 2-letter abbreviation
                if len(part) == 2 and part.upper() in _US_STATE_ABBREVS:
                    return True
                # Title-cased abbreviation like "Ny" or "Ca" (from normalize_location)
                if len(part) == 2 and part.upper() in _US_STATE_ABBREVS:
                    return True

    return False


def is_remote_job(title: str, location: str, description: str) -> bool:
    """Determine if a job is remote.
    
    Args:
        title: Job title.
        location: Job location.
        description: Job description.
    
    Returns:
        True if job appears to be remote.
    """
    combined = (title + " " + location + " " + description).lower()
    
    remote_keywords = ["remote", "work from home", "wfh", "distributed", "anywhere"]
    return any(keyword in combined for keyword in remote_keywords)
