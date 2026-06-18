"""Schemas for normalized job data.

Defines the unified NormalizedJob schema that all adapters normalize to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional


@dataclass
class NormalizedJob:
    """Unified job format stored in the jobs table.
    
    All sources (Greenhouse, Lever, Indeed, LinkedIn, RSS, etc.) normalize to this schema.
    This is the output of Submodule 2 (Normalization) and input to downstream modules.
    """
    
    # Core fields (required)
    title: str
    company: str
    location: str  # e.g., "Remote", "New York, NY", "USA"
    url: str  # Original job posting URL
    description: str
    source: str  # Adapter platform name (e.g., "greenhouse", "lever", "rss_generic")
    
    # Extracted fields
    skills: List[str] = field(default_factory=list)  # Extracted keywords
    salary_min: Optional[int] = None  # Minimum salary in cents or dollars
    salary_max: Optional[int] = None  # Maximum salary
    pay_period: Literal["hourly", "yearly"] = "yearly"
    job_type: Literal["full-time", "contract", "part-time", "temporary", "internship"] = "full-time"
    
    # Metadata
    posted_at: datetime = field(default_factory=datetime.utcnow)
    canonical_url: str = ""  # URL with tracking params stripped
    embedding: Optional[List[float]] = None  # 1536-dim vector (populated by Submodule 6)
    source_url: str = ""  # Same as url (for clarity)
    raw_source: Optional[Dict[str, Any]] = None  # Reference to raw adapter data
    
    # Internal tracking
    job_id: Optional[str] = None  # Assigned on storage (Module 1)
    normalized_at: datetime = field(default_factory=datetime.utcnow)
    
    def __post_init__(self):
        """Validate and normalize fields."""
        if not self.title or not self.company or not self.url:
            raise ValueError("NormalizedJob requires title, company, and url")
        
        if not self.source_url:
            self.source_url = self.url
        
        if not self.canonical_url:
            self.canonical_url = self.url
        
        # Normalize title and company
        self.title = self.title.strip()
        self.company = self.company.strip()
        self.location = self.location.strip() if self.location else "Unknown"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "url": self.url,
            "description": self.description,
            "source": self.source,
            "skills": self.skills,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "pay_period": self.pay_period,
            "job_type": self.job_type,
            "posted_at": self.posted_at.isoformat(),
            "canonical_url": self.canonical_url,
            "embedding": self.embedding,
            "job_id": self.job_id,
        }
    
    def __repr__(self) -> str:
        salary = f"${self.salary_min}-${self.salary_max}/{self.pay_period}" if self.salary_min else "Unknown"
        return f"NormalizedJob(title={self.title!r}, company={self.company!r}, salary={salary}, type={self.job_type})"
