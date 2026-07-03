from __future__ import annotations

from typing import Any, Optional, List, Literal
from datetime import datetime, timezone
from pydantic import BaseModel, Field, ConfigDict, model_validator


class NormalizedJob(BaseModel):
    """Minimal normalized job schema compatible with module3 expectations."""

    model_config = ConfigDict(extra="allow")

    title: str
    company: str
    location: Optional[str] = "Remote"
    source: Optional[str] = "unknown"
    source_url: Optional[str] = ""
    canonical_url: Optional[str] = ""
    description: Optional[str] = ""
    skills: Optional[List[str]] = Field(default_factory=list)
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    pay_period: Literal["hourly", "yearly", "monthly", "daily", "unknown"] = "yearly"
    job_type: Literal["full-time", "contract", "part-time", "full time", "full_time", "unknown"] = "full-time"
    posted_at: Optional[datetime] = None
    embedding: Optional[List[float]] = None
    job_id: Optional[str] = None
    url: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Normalize pay_period
            pay_period = str(data.get("pay_period") or "").lower().strip()
            if pay_period not in ["hourly", "yearly", "monthly", "daily"]:
                pay_period = "unknown"
            data["pay_period"] = pay_period

            # Normalize job_type
            job_type = str(data.get("job_type") or "").lower().strip().replace("_", "-").replace(" ", "-")
            if job_type not in ["full-time", "contract", "part-time", "full-time", "full_time"]:
                if "full" in job_type:
                    job_type = "full-time"
                elif "part" in job_type:
                    job_type = "part-time"
                elif "contract" in job_type or "temp" in job_type:
                    job_type = "contract"
                else:
                    job_type = "unknown"
            data["job_type"] = job_type
        return data

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
