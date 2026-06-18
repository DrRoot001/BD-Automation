"""Quick tests for Submodule 2: Normalization Engine."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from module2.adapters import RawJobData
from module2.normalization import normalize, NormalizedJob
from module2.extraction.salary_parser import parse_salary
from module2.extraction.skills_extractor import extract_skills
from module2.extraction.url_canonicalizer import canonicalize_url


def test_normalize_basic():
    """Test basic normalization."""
    print("\n✓ Test 1: Basic Normalization")
    
    raw = RawJobData(
        title="  Senior Python Engineer  ",
        company="Acme Corp",
        location="Remote",
        url="https://example.com/jobs/123",
        description="Looking for Python and AI expertise",
    )
    
    normalized = normalize(raw)
    
    assert normalized.title == "Senior Python Engineer", f"Title not cleaned: {normalized.title}"
    assert normalized.job_type == "full-time", f"Wrong job type: {normalized.job_type}"
    assert isinstance(normalized, NormalizedJob), "Should return NormalizedJob"
    print(f"  ✓ Normalized: {normalized.title} at {normalized.company}")


def test_salary_parsing():
    """Test salary parsing."""
    print("\n✓ Test 2: Salary Parsing")
    
    test_cases = [
        ("$60/hour", (60, None, "hourly")),
        ("$80,000 - $120,000 per year", (80000, 120000, "yearly")),
        ("$120k - $150k", (120000, 150000, "yearly")),
        ("competitive", (None, None, "yearly")),
    ]
    
    for salary_text, expected in test_cases:
        result = parse_salary(salary_text)
        assert result == expected, f"Failed for {salary_text}: got {result}, expected {expected}"
        print(f"  ✓ '{salary_text}' → {result}")


def test_skills_extraction():
    """Test skills extraction."""
    print("\n✓ Test 3: Skills Extraction")
    
    title = "ML Engineer"
    description = "Expert in Python, Machine Learning, TensorFlow, and AWS"
    
    skills = extract_skills(title, description)
    
    assert len(skills) > 0, "Should extract at least one skill"
    assert "python" in skills, f"Should detect Python, got {skills}"
    assert "machine learning" in skills or "ml" in skills, f"Should detect ML, got {skills}"
    print(f"  ✓ Extracted skills: {skills[:5]}")


def test_url_canonicalization():
    """Test URL canonicalization."""
    print("\n✓ Test 4: URL Canonicalization")
    
    test_cases = [
        (
            "https://example.com/jobs/123?utm_source=linkedin&utm_medium=social",
            "https://example.com/jobs/123"
        ),
        (
            "https://EXAMPLE.COM/JOBS/123/",
            "https://example.com/jobs/123"
        ),
    ]
    
    for url, expected in test_cases:
        canonical = canonicalize_url(url)
        assert canonical == expected, f"Failed for {url}: got {canonical}, expected {expected}"
        print(f"  ✓ Canonical: ...{canonical[-30:]}")


def test_job_type_classification():
    """Test job type classification."""
    print("\n✓ Test 5: Job Type Classification")
    
    from module2.normalization.helpers import classify_job_type
    
    test_cases = [
        ("Contract Developer", "Some description", "contract"),
        ("Part-time Writer", "Work 20 hours/week", "part-time"),
        ("Senior Software Engineer", "Full-time role", "full-time"),
        ("Internship", "Summer internship program", "internship"),
    ]
    
    for title, desc, expected_type in test_cases:
        job_type = classify_job_type(title, desc)
        assert job_type == expected_type, f"Failed for {title}: got {job_type}, expected {expected_type}"
        print(f"  ✓ '{title}' → {job_type}")


def test_location_normalization():
    """Test location normalization."""
    print("\n✓ Test 6: Location Normalization")
    
    from module2.normalization.helpers import normalize_location
    
    test_cases = [
        ("remote", "Remote"),
        ("work from home", "Remote"),
        ("usa", "USA"),
        ("new york, ny", "New York, NY"),
    ]
    
    for location, expected in test_cases:
        normalized = normalize_location(location)
        assert normalized == expected, f"Failed for {location}: got {normalized}, expected {expected}"
        print(f"  ✓ '{location}' → '{normalized}'")


def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("Submodule 2: Normalization Engine - Quick Tests")
    print("="*80)
    
    try:
        test_salary_parsing()
        test_skills_extraction()
        test_url_canonicalization()
        test_location_normalization()
        test_job_type_classification()
        test_normalize_basic()
        
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
    exit_code = main()
    sys.exit(exit_code)
