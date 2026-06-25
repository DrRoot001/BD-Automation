"""Module 2: Job Discovery & Normalization Engine

This module handles:
- Multi-platform job discovery (RSS, APIs, scrapers)
- Raw job normalization to unified schema
- Skills and salary extraction
- 3-layer deduplication (URL → title+company → embedding)
- Business rule filtering and matching scoring
- Event publishing for downstream modules
"""

__version__ = "0.1.0"
