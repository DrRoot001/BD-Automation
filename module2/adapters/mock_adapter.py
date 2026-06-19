"""Mock adapter for testing and development.

Returns hardcoded sample jobs without making real API calls.
Useful for integration testing and demos.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from .base import BaseSourceAdapter, RawJobData
from .registry import register_adapter


@register_adapter
class MockAdapter(BaseSourceAdapter):
    """Mock adapter that returns hardcoded sample jobs.
    
    Useful for testing the entire pipeline without external API calls.
    """
    
    platform_name = "mock"
    ingestion_type = "api"
    
    async def discover_jobs(self, filters: Dict[str, Any]) -> List[RawJobData]:
        """Return sample jobs for testing."""
        now = datetime.now(timezone.utc)
        
        sample_jobs = [
            RawJobData(
                title="Senior AI Automation Engineer",
                company="TechCorp",
                location="Remote, USA",
                url="https://techcorp.com/jobs/ai-engineer-1",
                description="We are hiring an AI Automation Engineer with 3+ years experience in ML ServiceNow and AI automation.",
                salary_text="$120,000 - $150,000 per year",
                posted_at=now - timedelta(hours=2),
                source_platform=self.platform_name,
            ),
            RawJobData(
                title="ML Service Now Developer (Contract)",
                company="AutoSoft",
                location="Remote",
                url="https://autosoft.com/jobs/ml-sn-dev",
                description="Contract role: Develop ML solutions on ServiceNow platform. $65/hour. 6 months.",
                salary_text="$65 per hour",
                posted_at=now - timedelta(hours=5),
                source_platform=self.platform_name,
            ),
            RawJobData(
                title="AI Automation Specialist (Part-Time)",
                company="DataFlow Inc",
                location="USA",
                url="https://dataflow.com/jobs/ai-automation-pt",
                description="Part-time opportunity: Implement AI automation workflows. Requires Python, Machine Learning, AI knowledge.",
                salary_text="$60/hour",
                posted_at=now - timedelta(days=1),
                source_platform=self.platform_name,
            ),
            RawJobData(
                title="Python Backend Engineer",
                company="WebStack Co",
                location="New York, NY",
                url="https://webstack.com/jobs/python-backend",
                description="Full-time: Build backend services with Python. AWS, Docker, Kubernetes.",
                salary_text="$130,000",
                posted_at=now - timedelta(hours=12),
                source_platform=self.platform_name,
            ),
        ]
        
        return sample_jobs
    
    async def get_job_detail(self, job_url: str) -> RawJobData:
        """Return sample job detail."""
        return RawJobData(
            title="Sample Job",
            company="Sample Co",
            location="Remote",
            url=job_url,
            description="This is a sample job detail.",
            source_platform=self.platform_name,
        )
