"""Example usage of Submodule 2: Normalization Engine.

Shows how to normalize raw job data into the unified schema.
"""

import asyncio
from datetime import datetime, timedelta

from module2.adapters import get_adapter
from module2.normalization import normalize, normalize_batch


async def example_normalize_mock_adapter():
    """Example 1: Discover and normalize jobs from MockAdapter."""
    print("\n" + "="*80)
    print("Example 1: Normalize jobs from MockAdapter")
    print("="*80)
    
    # Discover raw jobs
    adapter = get_adapter("mock")()
    raw_jobs = await adapter.discover_jobs({})
    
    print(f"Discovered {len(raw_jobs)} raw jobs")
    
    # Normalize them
    normalized_jobs = normalize_batch(raw_jobs)
    
    print(f"Normalized {len(normalized_jobs)} jobs:\n")
    
    for job in normalized_jobs:
        print(f"Title: {job.title}")
        print(f"Company: {job.company}")
        print(f"Location: {job.location}")
        print(f"Type: {job.job_type}")
        print(f"Source: {job.source}")
        print(f"Skills: {', '.join(job.skills) if job.skills else 'None'}")
        if job.salary_min:
            salary_display = f"${job.salary_min}"
            if job.salary_max:
                salary_display += f" - ${job.salary_max}"
            salary_display += f" ({job.pay_period})"
            print(f"Salary: {salary_display}")
        print(f"Posted: {job.posted_at}")
        print()


async def example_normalize_single():
    """Example 2: Normalize a single raw job."""
    print("\n" + "="*80)
    print("Example 2: Normalize a single raw job")
    print("="*80)
    
    from module2.adapters import RawJobData
    
    # Create a raw job
    raw_job = RawJobData(
        title="Senior ML Service Now Developer (Contract)",
        company="TechCorp Inc",
        location="Remote, USA",
        url="https://techcorp.com/jobs/123?utm_source=linkedin&utm_medium=social",
        description="""
            We are seeking an experienced ML Service Now developer with 5+ years 
            of expertise in AI automation and ServiceNow. 
            
            Requirements:
            - Python, JavaScript
            - AWS, Docker, Kubernetes
            - Experience with Machine Learning and NLP
            
            Salary: $80,000 - $120,000 per year (contract)
        """,
        salary_text="$80,000 - $120,000 per year",
        posted_at=datetime.utcnow() - timedelta(hours=2),
        source_platform="greenhouse",
    )
    
    # Normalize it
    normalized = normalize(raw_job)
    
    print(f"Original URL: {raw_job.url}")
    print(f"Canonical URL: {normalized.canonical_url}")
    print()
    print(f"Title: {normalized.title}")
    print(f"Company: {normalized.company}")
    print(f"Location: {normalized.location}")
    print(f"Job Type: {normalized.job_type}")
    print(f"Salary: ${normalized.salary_min} - ${normalized.salary_max} ({normalized.pay_period})")
    print(f"Skills: {', '.join(normalized.skills)}")
    print(f"Description (normalized):\n{normalized.description[:100]}...")


async def main():
    """Run all examples."""
    print("\n" + "#"*80)
    print("# Submodule 2: Normalization Engine - Examples")
    print("#"*80)
    
    await example_normalize_mock_adapter()
    await example_normalize_single()
    
    print("\n" + "#"*80)
    print("# Examples complete!")
    print("#"*80)


if __name__ == "__main__":
    asyncio.run(main())
