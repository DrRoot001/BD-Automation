"""Submodules 3, 4, 5: Skills & Salary Extraction + URL Canonicalization

Extract and normalize skills, salary, and URLs from job data.
"""

from .salary_parser import parse_salary, format_salary
from .skills_extractor import extract_skills, skill_match_ratio, get_taxonomy
from .url_canonicalizer import canonicalize_url, url_hash, are_urls_equivalent

__all__ = [
    "parse_salary",
    "format_salary",
    "extract_skills",
    "skill_match_ratio",
    "get_taxonomy",
    "canonicalize_url",
    "url_hash",
    "are_urls_equivalent",
]
