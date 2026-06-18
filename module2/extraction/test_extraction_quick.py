"""Tests for Submodules 3, 4, 5: Extraction (Skills, Salary, URLs)."""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from module2.extraction.salary_parser import parse_salary, format_salary
from module2.extraction.skills_extractor import extract_skills, skill_match_ratio
from module2.extraction.url_canonicalizer import canonicalize_url, url_hash, are_urls_equivalent


def test_salary_parser():
    """Test salary parsing."""
    print("\n✓ Test Salary Parser")
    
    tests = [
        ("$60/hour", (60, None, "hourly")),
        ("$60 per hour", (60, None, "hourly")),
        ("$80,000 - $120,000 per year", (80000, 120000, "yearly")),
        ("$120k - $150k", (120000, 150000, "yearly")),
        ("$150,000", (150000, None, "yearly")),
        ("150k", (150000, None, "yearly")),
        ("competitive", (None, None, "yearly")),
        ("", (None, None, "yearly")),
    ]
    
    for salary_text, expected in tests:
        result = parse_salary(salary_text)
        assert result == expected, f"'{salary_text}' → {result}, expected {expected}"
        print(f"  ✓ '{salary_text}' → {result}")


def test_salary_format():
    """Test salary formatting."""
    print("\n✓ Test Salary Format")
    
    tests = [
        ((60, None, "hourly"), "$60/hour"),
        ((120000, 150000, "yearly"), "$120k - $150k/year"),
    ]
    
    for (min_sal, max_sal, period), expected_display in tests:
        result = format_salary(min_sal, max_sal, period)
        assert expected_display in result or result == expected_display, \
            f"Format failed for {min_sal}, {max_sal}, {period}"
        print(f"  ✓ ({min_sal}, {max_sal}, {period}) → '{result}'")


def test_skills_extraction():
    """Test skills extraction."""
    print("\n✓ Test Skills Extraction")
    
    tests = [
        ("Python Developer", "Python and Django", ["python"]),
        ("ML Engineer", "Machine Learning, NLP, Python", ["python", "machine learning"]),
        ("DevOps", "Docker, Kubernetes, AWS", ["docker", "kubernetes", "aws"]),
    ]
    
    for title, desc, expected_skills in tests:
        skills = extract_skills(title, desc)
        for expected in expected_skills:
            assert expected in skills, f"Should find '{expected}' in {skills}"
        print(f"  ✓ '{title}' → {skills[:3]}...")


def test_skill_matching():
    """Test skill matching ratio."""
    print("\n✓ Test Skill Matching")
    
    required = ["python", "javascript", "aws"]
    job_skills = ["python", "javascript", "docker", "kubernetes"]
    
    ratio = skill_match_ratio(required, job_skills)
    assert 0.6 <= ratio <= 0.8, f"Ratio should be ~2/3, got {ratio}"
    print(f"  ✓ Match ratio: {ratio:.1%} (2 of 3 required skills found)")


def test_url_canonicalization():
    """Test URL canonicalization."""
    print("\n✓ Test URL Canonicalization")
    
    tests = [
        (
            "https://example.com/jobs/123?utm_source=linkedin&utm_medium=social",
            "https://example.com/jobs/123"
        ),
        (
            "https://EXAMPLE.COM/JOBS/123/",
            "https://example.com/jobs/123"
        ),
        (
            "https://example.com/jobs/123?page=1&utm_source=ref",
            "https://example.com/jobs/123?page=1"
        ),
    ]
    
    for url, expected in tests:
        result = canonicalize_url(url)
        assert result == expected, f"{url} → {result}, expected {expected}"
        print(f"  ✓ Canonical: ...{result[-40:]}")


def test_url_hash():
    """Test URL hashing."""
    print("\n✓ Test URL Hash")
    
    url1 = "https://example.com/jobs/123?utm_source=linkedin"
    url2 = "https://example.com/jobs/123?utm_source=indeed"
    
    hash1 = url_hash(url1)
    hash2 = url_hash(url2)
    
    assert len(hash1) == 64, "SHA256 should be 64 hex chars"
    assert hash1 == hash2, "Same canonical URL should have same hash"
    print(f"  ✓ URL hash length: {len(hash1)} chars")
    print(f"  ✓ Tracking params stripped, hashes match")


def test_url_equivalence():
    """Test URL equivalence checking."""
    print("\n✓ Test URL Equivalence")
    
    url1 = "https://example.com/jobs/123"
    url2 = "https://EXAMPLE.COM/jobs/123/?utm_source=google"
    url3 = "https://example.com/jobs/456"
    
    assert are_urls_equivalent(url1, url2), "Should be equivalent"
    assert not are_urls_equivalent(url1, url3), "Should not be equivalent"
    print(f"  ✓ URL1 and URL2 are equivalent (ignoring case and tracking)")
    print(f"  ✓ URL1 and URL3 are not equivalent (different job IDs)")


def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("Submodules 3, 4, 5: Extraction Tests (Skills, Salary, URLs)")
    print("="*80)
    
    try:
        test_salary_parser()
        test_salary_format()
        test_skills_extraction()
        test_skill_matching()
        test_url_canonicalization()
        test_url_hash()
        test_url_equivalence()
        
        print("\n" + "="*80)
        print("✓ All extraction tests passed!")
        print("="*80)
        return 0
    
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
