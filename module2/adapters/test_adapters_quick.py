"""Quick test of Submodule 1 adapters.

Run with: python test_adapters_quick.py
"""

import asyncio
import sys
from pathlib import Path
import pytest
pytestmark = pytest.mark.asyncio

# Add module2 to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters import get_adapter, list_adapters
from adapters.mock_adapter import MockAdapter
from adapters.rss_adapter import RssAdapter
from adapters.greenhouse_adapter import GreenhouseAdapter
from adapters.lever_adapter import LeverAdapter


async def test_mock_adapter():
    """Test MockAdapter with sample data."""
    print("\n✓ Test 1: MockAdapter")
    adapter = get_adapter("mock")()
    assert adapter is not None, "MockAdapter not registered"
    
    filters = {"location": "USA"}
    jobs = await adapter.discover_jobs(filters)
    
    assert len(jobs) > 0, "MockAdapter should return sample jobs"
    assert jobs[0].title, "Job should have title"
    assert jobs[0].url, "Job should have URL"
    print(f"  ✓ MockAdapter returned {len(jobs)} sample jobs")
    print(f"  ✓ First job: {jobs[0].title} at {jobs[0].company}")


async def test_adapter_registry():
    """Test that adapters are properly registered."""
    print("\n✓ Test 2: Adapter Registry")
    adapters = list_adapters()
    assert len(adapters) > 0, "No adapters registered"
    print(f"  ✓ Registered adapters: {list(adapters.keys())}")


async def test_greenhouse_adapter():
    """Test GreenhouseAdapter instantiation."""
    print("\n✓ Test 3: GreenhouseAdapter Instantiation")
    adapter = get_adapter("greenhouse")()
    assert adapter is not None, "GreenhouseAdapter not registered"
    assert adapter.platform_name == "greenhouse", "Wrong platform name"
    print(f"  ✓ GreenhouseAdapter ready: {adapter}")


async def test_lever_adapter():
    """Test LeverAdapter instantiation."""
    print("\n✓ Test 4: LeverAdapter Instantiation")
    adapter = get_adapter("lever")()
    assert adapter is not None, "LeverAdapter not registered"
    assert adapter.platform_name == "lever", "Wrong platform name"
    print(f"  ✓ LeverAdapter ready: {adapter}")


async def test_rss_adapter():
    """Test RssAdapter instantiation."""
    print("\n✓ Test 5: RssAdapter Instantiation")
    adapter = get_adapter("rss_generic")()
    assert adapter is not None, "RssAdapter not registered"
    assert adapter.platform_name == "rss_generic", "Wrong platform name"
    print(f"  ✓ RssAdapter ready: {adapter}")


async def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("Module 2, Submodule 1: Quick Tests")
    print("="*80)
    
    try:
        await test_adapter_registry()
        await test_mock_adapter()
        await test_greenhouse_adapter()
        await test_lever_adapter()
        await test_rss_adapter()
        
        print("\n" + "="*80)
        print("✓ All tests passed!")
        print("="*80)
        return 0
    
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
