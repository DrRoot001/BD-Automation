"""Normalization engine: RawJobData → NormalizedJob.

This module coordinates all normalization steps:
1. Text cleaning (title, company, location)
2. Job type classification
3. Salary extraction (via extraction module)
4. Skills extraction (via extraction module)
5. URL canonicalization
6. Assemble into NormalizedJob
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from module2.adapters import RawJobData
from module2.extraction.salary_parser import parse_salary
from module2.extraction.skills_extractor import extract_skills
from module2.extraction.url_canonicalizer import canonicalize_url

from .helpers import (
    classify_job_type,
    clean_title,
    extract_required_years,
    is_remote_job,
    normalize_location,
    normalize_text,
)
from .schemas import NormalizedJob


class Normalizer:
    """Main normalization engine.
    
    Converts RawJobData (from adapters) into NormalizedJob (unified schema).
    """
    
    def __init__(self):
        """Initialize normalizer."""
        pass
    
    def normalize(self, raw_job: RawJobData) -> NormalizedJob:
        """Normalize a raw job into unified schema.
        
        Args:
            raw_job: RawJobData from a source adapter.
        
        Returns:
            NormalizedJob with all fields extracted and normalized.
        
        Raises:
            ValueError: If required fields are missing or invalid.
        """
        # Validate required fields
        if not raw_job.title or not raw_job.url:
            raise ValueError("RawJobData requires title and url")
        
        # Step 1: Clean title and company
        title = clean_title(raw_job.title)
        company = normalize_text(raw_job.company or "Unknown")
        
        # Step 2: Normalize location
        location = normalize_location(raw_job.location or "")
        
        # Step 3: Classify job type
        job_type = classify_job_type(
            title,
            raw_job.description or ""
        )
        
        # Step 4: Parse salary
        salary_min, salary_max, pay_period = parse_salary(raw_job.salary_text)
        
        # Step 5: Extract skills
        skills = extract_skills(
            title,
            raw_job.description or ""
        )
        
        # Step 6: Canonicalize URL
        canonical_url = canonicalize_url(raw_job.url)
        
        # Step 7: Assemble NormalizedJob
        normalized = NormalizedJob(
            title=title,
            company=company,
            location=location,
            url=raw_job.url,
            source_url=raw_job.url,
            canonical_url=canonical_url,
            description=normalize_text(raw_job.description or ""),
            source=raw_job.source_platform,
            skills=skills,
            salary_min=salary_min,
            salary_max=salary_max,
            pay_period=pay_period,
            job_type=job_type,
            posted_at=raw_job.posted_at or datetime.utcnow(),
            raw_source=raw_job.raw_json,
        )
        
        return normalized
    
    def normalize_batch(self, raw_jobs: list[RawJobData]) -> list[NormalizedJob]:
        """Normalize multiple raw jobs.
        
        Args:
            raw_jobs: List of RawJobData objects.
        
        Returns:
            List of NormalizedJob objects. Errors are skipped with logging.
        """
        normalized_jobs = []
        
        for raw_job in raw_jobs:
            try:
                normalized = self.normalize(raw_job)
                normalized_jobs.append(normalized)
            except Exception as e:
                print(f"Error normalizing job {raw_job.title}: {e}")
                continue
        
        return normalized_jobs


# Global normalizer instance
_normalizer = None


def get_normalizer() -> Normalizer:
    """Get or create the global normalizer instance.
    
    Returns:
        Normalizer instance.
    """
    global _normalizer
    if _normalizer is None:
        _normalizer = Normalizer()
    return _normalizer


def normalize(raw_job: RawJobData) -> NormalizedJob:
    """Convenience function to normalize a single job.
    
    Args:
        raw_job: RawJobData to normalize.
    
    Returns:
        NormalizedJob.
    """
    return get_normalizer().normalize(raw_job)


def normalize_batch(raw_jobs: list[RawJobData]) -> list[NormalizedJob]:
    """Convenience function to normalize multiple jobs.
    
    Args:
        raw_jobs: List of RawJobData objects.
    
    Returns:
        List of NormalizedJob objects.
    """
    return get_normalizer().normalize_batch(raw_jobs)
