"""Extract candidate facts from the resume PDF and use them to fill gaps
in the candidate_profile dict.

The browser_automation pipeline builds `candidate_profile` from the M1
candidates DB. When that DB row is thin (e.g. location is just `"US"` or
empty), the AgentLoop and the deterministic policy have nothing to anchor
on for fields like "Location (City)" or "what state are you working from?".

This module fills those gaps by scanning the resume PDF text directly. The
resume is the SOURCE OF TRUTH for the candidate — whatever their actual city
and state are, they'll be printed in the resume header. We extract those and
merge them into the profile dict (without overwriting fields that are
already populated with real data).

This is candidate-agnostic by design: no hardcoded city/state defaults, no
per-candidate special cases. Plug a different resume in and the enricher
produces different output.

Public API:
    enrich_profile_from_resume(profile: dict, resume_local_path: str) -> dict
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# US state name → abbr and abbr → name maps. Used to recognize state tokens
# in resume header text.
_STATE_ABBR_TO_NAME: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
_STATE_NAME_TO_ABBR: dict[str, str] = {
    name.lower(): abbr for abbr, name in _STATE_ABBR_TO_NAME.items()
}

# Major US city → (state name, state abbr). Used to handle resume headers
# that show only "City, US" (or "City, Country") — common when the resume
# author wants to emphasize country over state. Keep this list small and
# add to it as new cities show up in real resumes.
_US_CITY_TO_STATE: dict[str, Tuple[str, str]] = {
    "san francisco":  ("California", "CA"),
    "los angeles":    ("California", "CA"),
    "san diego":      ("California", "CA"),
    "san jose":       ("California", "CA"),
    "oakland":        ("California", "CA"),
    "sacramento":     ("California", "CA"),
    "new york":       ("New York", "NY"),
    "new york city":  ("New York", "NY"),
    "brooklyn":       ("New York", "NY"),
    "manhattan":      ("New York", "NY"),
    "queens":         ("New York", "NY"),
    "bronx":          ("New York", "NY"),
    "nyc":            ("New York", "NY"),
    "seattle":        ("Washington", "WA"),
    "tacoma":         ("Washington", "WA"),
    "austin":         ("Texas", "TX"),
    "houston":        ("Texas", "TX"),
    "dallas":         ("Texas", "TX"),
    "san antonio":    ("Texas", "TX"),
    "chicago":        ("Illinois", "IL"),
    "boston":         ("Massachusetts", "MA"),
    "cambridge":      ("Massachusetts", "MA"),
    "miami":          ("Florida", "FL"),
    "orlando":        ("Florida", "FL"),
    "tampa":          ("Florida", "FL"),
    "atlanta":        ("Georgia", "GA"),
    "denver":         ("Colorado", "CO"),
    "boulder":        ("Colorado", "CO"),
    "portland":       ("Oregon", "OR"),
    "philadelphia":   ("Pennsylvania", "PA"),
    "pittsburgh":     ("Pennsylvania", "PA"),
    "washington":     ("District of Columbia", "DC"),
    "phoenix":        ("Arizona", "AZ"),
    "tucson":         ("Arizona", "AZ"),
    "minneapolis":    ("Minnesota", "MN"),
    "detroit":        ("Michigan", "MI"),
    "ann arbor":      ("Michigan", "MI"),
    "nashville":      ("Tennessee", "TN"),
    "raleigh":        ("North Carolina", "NC"),
    "charlotte":      ("North Carolina", "NC"),
    "salt lake city": ("Utah", "UT"),
}
_COUNTRY_TOKENS = {"us", "usa", "u.s.", "u.s.a.", "united states", "america"}


def _read_resume_text(resume_local_path: str, max_chars: int = 4000) -> str:
    """Extract text from a resume PDF (first ~4k chars — enough for the header).

    Falls back gracefully if extraction fails — returns empty string and the
    caller skips enrichment.
    """
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        logger.warning("[ResumeEnrich] pdfplumber not installed; skipping enrichment")
        return ""
    try:
        with pdfplumber.open(resume_local_path) as pdf:
            parts: list[str] = []
            for page in pdf.pages[:2]:  # only need the first page or two
                t = page.extract_text() or ""
                parts.append(t)
                if sum(len(p) for p in parts) >= max_chars:
                    break
            return "\n".join(parts)[:max_chars]
    except Exception as exc:
        logger.warning(f"[ResumeEnrich] Failed to read {resume_local_path!r}: {exc}")
        return ""


_LOCATION_PREFIXES = (
    "based in", "located in", "currently in", "living in", "from",
    "residence:", "address:", "location:",
)


def _clean_city(city: str) -> str:
    """Strip location-introducer phrases ("Based in Seattle" → "Seattle")."""
    s = city.strip(" .,-'")
    low = s.lower()
    for pre in _LOCATION_PREFIXES:
        if low.startswith(pre + " ") or low.startswith(pre):
            s = s[len(pre):].strip(" .,-'")
            low = s.lower()
    return s


def _extract_us_location(text: str) -> Optional[Tuple[str, str, str]]:
    """Find a US location in the text. Returns (city, state_name, state_abbr).

    Strategy — first match wins:
      1. ``City, ST`` two-letter pattern (most common header format).
      2. ``City, State Name`` long-form pattern.
    Matches are case-insensitive but we preserve the original casing of the
    city as it appeared in the resume.
    """
    if not text:
        return None

    # Pattern 1: City Name, ST  (ST is a real US state abbr OR a US country token)
    # Captures both "San Francisco, CA" and "New York, US" — the latter
    # gets resolved through _US_CITY_TO_STATE.
    abbr_pattern = re.compile(
        r"\b([A-Z][A-Za-z\.\-' ]{1,30}?)\s*,\s*([A-Za-z]{2,3})\b"
    )
    for m in abbr_pattern.finditer(text):
        city_raw = _clean_city(m.group(1))
        second = m.group(2)
        second_upper = second.upper()
        # 1a: real state code
        if second_upper in _STATE_ABBR_TO_NAME and len(city_raw) >= 2:
            return (city_raw, _STATE_ABBR_TO_NAME[second_upper], second_upper)
        # 1b: country token — fall back to the city-to-state map
        if second.lower() in _COUNTRY_TOKENS and len(city_raw) >= 2:
            mapped = _US_CITY_TO_STATE.get(city_raw.lower())
            if mapped:
                state_name, abbr = mapped
                return (city_raw, state_name, abbr)

    # Pattern 2: City, State Name (e.g. "San Francisco, California")
    state_names_alt = "|".join(
        re.escape(n) for n in sorted(_STATE_NAME_TO_ABBR.keys(), key=len, reverse=True)
    )
    name_pattern = re.compile(
        rf"\b([A-Z][A-Za-z\.\-' ]{{1,30}}?)\s*,\s*({state_names_alt})\b",
        re.IGNORECASE,
    )
    for m in name_pattern.finditer(text):
        city_raw = _clean_city(m.group(1))
        state_lower = m.group(2).lower()
        if state_lower in _STATE_NAME_TO_ABBR and len(city_raw) >= 2:
            abbr = _STATE_NAME_TO_ABBR[state_lower]
            return (city_raw, _STATE_ABBR_TO_NAME[abbr], abbr)

    # Pattern 3: City, United States  /  City, USA  (verbose country form)
    country_alt = "|".join(re.escape(c) for c in sorted(_COUNTRY_TOKENS, key=len, reverse=True))
    verbose_pattern = re.compile(
        rf"\b([A-Z][A-Za-z\.\-' ]{{1,30}}?)\s*,\s*({country_alt})\b",
        re.IGNORECASE,
    )
    for m in verbose_pattern.finditer(text):
        city_raw = _clean_city(m.group(1))
        mapped = _US_CITY_TO_STATE.get(city_raw.lower())
        if mapped and len(city_raw) >= 2:
            state_name, abbr = mapped
            return (city_raw, state_name, abbr)

    return None


def _is_too_generic(value: str) -> bool:
    """True if a string is empty or just a country code/name with no city."""
    s = (value or "").strip().lower()
    return s in {
        "", "us", "usa", "u.s.", "u.s.a.", "united states",
        "united states of america", "america",
    } or len(s) <= 2


def enrich_profile_from_resume(profile: dict, resume_local_path: str) -> dict:
    """Mutate `profile` in place — fill thin/missing fields from the resume PDF.

    Currently enriches:
      * ``location`` — only overwritten when the existing value is too
        generic (empty, country-code-only, ≤2 chars). When the DB already has
        a real city/state, we leave it alone.

    Returns the same dict for convenience.
    """
    text = _read_resume_text(resume_local_path)
    if not text:
        return profile

    current_loc = (profile.get("location") or "").strip()
    if _is_too_generic(current_loc):
        loc = _extract_us_location(text)
        if loc:
            city, state_name, state_abbr = loc
            new_value = f"{city}, {state_abbr}, USA"
            logger.info(
                f"[ResumeEnrich] location {current_loc!r} → {new_value!r} "
                f"(extracted from resume)"
            )
            profile["location"] = new_value
        else:
            logger.warning(
                f"[ResumeEnrich] location {current_loc!r} is generic but no US "
                f"city/state pattern found in resume — leaving as-is"
            )

    return profile
