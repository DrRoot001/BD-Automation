"""Fuzzy matching utilities for deduplication."""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Tuple


def similarity(a: str, b: str) -> float:
    a = (a or "").lower()
    b = (b or "").lower()
    return SequenceMatcher(None, a, b).ratio()


def title_company_score(title_a: str, company_a: str, title_b: str, company_b: str) -> float:
    """Compute a heuristic similarity score between two job postings."""
    title_sim = similarity(title_a, title_b)
    company_sim = similarity(company_a, company_b)
    # Give more weight to title
    return 0.7 * title_sim + 0.3 * company_sim
