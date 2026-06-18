"""Example usage of Module 2 Submodule 1 adapters.

This demonstrates how to use the source adapters to discover jobs.
Run with: python -m asyncio example_usage.py
"""

import asyncio
from datetime import datetime

# Import adapters (they auto-register on import)
from module2.adapters import (
    get_adapter,
    list_adapters,
    RawJobData,
)

# Also import concrete adapters to ensure they're registered
from module2.adapters.mock_adapter import MockAdapter
from module2.adapters.rss_adapter import RssAdapter
from module2.adapters.greenhouse_adapter import GreenhouseAdapter
from module2.adapters.lever_adapter import LeverAdapter


async def example_mock():
    """Example 1: Discover jobs using MockAdapter (no external calls)."""
    print("\n" + "="*80)
    print("Example 1: MockAdapter (Hardcoded Test Data)")
    print("="*80)
    
    adapter = get_adapter("mock")()
    filters = {
        "location": "USA",
        "remote_only": True,
        "job_types": ["contract", "part-time"],
        "min_hourly_rate": 60,
    }
    
    jobs = await adapter.discover_jobs(filters)
    print(f"Discovered {len(jobs)} jobs:")
    for job in jobs:
        print(f"\n  Title: {job.title}")
        print(f"  Company: {job.company}")
        print(f"  Location: {job.location}")
        print(f"  URL: {job.url}")
        print(f"  Salary: {job.salary_text}")
        print(f"  Posted: {job.posted_at}")


async def example_greenhouse():
    """Example 2: Discover jobs from Greenhouse (requires real API)."""
    print("\n" + "="*80)
    print("Example 2: GreenhouseAdapter (Real API)")
    print("="*80)
    
    adapter = get_adapter("greenhouse")()
    filters = {
        "company": "stripe",  # Example company
        "location": "USA",
        "remote_only": True,
    }
    
    print(f"Fetching jobs from Greenhouse for company: {filters['company']}...")
    try:
        jobs = await adapter.discover_jobs(filters)
        print(f"Discovered {len(jobs)} jobs:")
        for job in jobs[:3]:  # Show first 3
            print(f"\n  Title: {job.title}")
            print(f"  Company: {job.company}")
            print(f"  Location: {job.location}")
            print(f"  URL: {job.url[:50]}...")
    except Exception as e:
        print(f"  Error: {e}")


async def example_lever():
    """Example 3: Discover jobs from Lever (requires real API)."""
    print("\n" + "="*80)
    print("Example 3: LeverAdapter (Real API)")
    print("="*80)
    
    adapter = get_adapter("lever")()
    filters = {
        "company": "figma",  # Example company
        "location": "USA",
    }
    
    print(f"Fetching jobs from Lever for company: {filters['company']}...")
    try:
        jobs = await adapter.discover_jobs(filters)
        print(f"Discovered {len(jobs)} jobs:")
        for job in jobs[:3]:  # Show first 3
            print(f"\n  Title: {job.title}")
            print(f"  Company: {job.company}")
            print(f"  Location: {job.location}")
    except Exception as e:
        print(f"  Error: {e}")


async def example_rss():
    """Example 4: Discover jobs from RSS feed."""
    print("\n" + "="*80)
    print("Example 4: RssAdapter (RSS Feed)")
    print("="*80)
    
    adapter = get_adapter("rss_generic")()
    filters = {
        "rss_url": "https://weworkremotely.com/categories/remote-jobs/jobs.rss",
        "remote_only": True,
    }
    
    print("Fetching jobs from RSS feed...")
    try:
        jobs = await adapter.discover_jobs(filters)
        print(f"Discovered {len(jobs)} jobs:")
        for job in jobs[:3]:  # Show first 3
            print(f"\n  Title: {job.title}")
            print(f"  Company: {job.company}")
            print(f"  Location: {job.location}")
    except Exception as e:
        print(f"  Error: {e}")


async def list_all_adapters():
    """Show all registered adapters."""
    print("\n" + "="*80)
    print("Registered Adapters")
    print("="*80)
    
    adapters = list_adapters()
    for name, adapter_cls in adapters.items():
        instance = adapter_cls()
        print(f"  {name}: {instance}")


async def main():
    """Run all examples."""
    print("\n" + "#"*80)
    print("# Module 2, Submodule 1: Source Adapters - Usage Examples")
    print("#"*80)
    
    # Show all registered adapters
    await list_all_adapters()
    
    # Run examples (MockAdapter works offline; others need real APIs)
    await example_mock()
    
    # Uncomment to test with real APIs (requires network):
    # await example_greenhouse()
    # await example_lever()
    # await example_rss()
    
    print("\n" + "#"*80)
    print("# Examples complete!")
    print("#"*80)


if __name__ == "__main__":
    asyncio.run(main())
