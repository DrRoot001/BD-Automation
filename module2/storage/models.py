"""Simple persistence models for jobs (SQLite-backed in job_store).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class StoredJob:
    id: str
    title: str
    company: str
    location: str
    url: str
    canonical_url: str
    posted_at: Optional[datetime]
    inserted_at: datetime
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    pay_period: Optional[str] = None
    skills: Optional[list] = None
    source: Optional[str] = None
