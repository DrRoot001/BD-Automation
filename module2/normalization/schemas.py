from __future__ import annotations

from typing import Any, Optional, List, Literal
from datetime import datetime, timezone
from pydantic import BaseModel, Field, ConfigDict


class NormalizedJob(BaseModel):
    """Minimal normalized job schema compatible with module3 expectations."""

    model_config = ConfigDict(extra="allow")

    title: str
    company: str
    location: str = "Remote"
    source: str = "unknown"
    source_url: str = ""
    canonical_url: str = ""
    description: str = ""
    skills: List[str] = Field(default_factory=list)
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    pay_period: Literal["hourly", "yearly", "monthly", "daily", "unknown"] = "yearly"
    job_type: Literal["full-time", "contract", "part-time", "full time", "full_time", "unknown"] = "full-time"
    posted_at: Optional[datetime] = None
    embedding: Optional[List[float]] = None
    job_id: Optional[str] = None
    url: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NormalizedJob":
        payload = dict(data or {})
        if "url" in payload and "source_url" not in payload:
            payload["source_url"] = payload["url"]
        if "canonical_url" not in payload:
            payload["canonical_url"] = payload.get("source_url", "")
        if "posted_at" in payload and isinstance(payload["posted_at"], str):
            try:
                payload["posted_at"] = datetime.fromisoformat(payload["posted_at"])
            except ValueError:
                payload["posted_at"] = datetime.now(timezone.utc)
        return cls(**payload)


def normalize_job(raw_job: Any) -> NormalizedJob:
    if isinstance(raw_job, NormalizedJob):
        return raw_job
    if isinstance(raw_job, dict):
        return NormalizedJob.from_dict(raw_job)
    if hasattr(raw_job, "model_dump"):
        return NormalizedJob.from_dict(raw_job.model_dump())
    raise TypeError(f"Unsupported job payload: {type(raw_job)!r}")


def normalize_batch(raw_jobs: list[Any]) -> list[NormalizedJob]:
    return [normalize_job(job) for job in raw_jobs]
