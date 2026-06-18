"""Example usage of Submodules 3, 4, 5: Extraction (Skills, Salary, URLs)."""

from extraction.salary_parser import parse_salary, format_salary
from extraction.skills_extractor import extract_skills, skill_match_ratio, get_taxonomy
from extraction.url_canonicalizer import canonicalize_url, url_hash, are_urls_equivalent


def example_salary_extraction():
    """Example 1: Extract salary from various formats."""
    print("\n" + "="*80)
    print("Example 1: Salary Extraction")
    print("="*80)
    
    salary_texts = [
        "$80,000 - $120,000 per year",
        "$60/hour",
        "$120k - $150k",
        "Competitive",
        "$50 - $75 per hour",
    ]
    
    for text in salary_texts:
        salary_min, salary_max, period = parse_salary(text)
        formatted = format_salary(salary_min, salary_max, period)
        print(f"\nInput:  '{text}'")
        print(f"Parsed: min=${salary_min}, max=${salary_max}, period={period}")
        print(f"Format: {formatted}")


def example_skills_extraction():
    """Example 2: Extract skills from job description."""
    print("\n" + "="*80)
    print("Example 2: Skills Extraction")
    print("="*80)
    
    job_title = "Senior ML Service Now Developer"
    job_description = """
    We're looking for an experienced developer with:
    - 5+ years of Python and JavaScript experience
    - Strong background in Machine Learning and AI automation
    - Experience with ServiceNow platform
    - AWS, Docker, and Kubernetes expertise
    - PostgreSQL and Redis database knowledge
    """
    
    print(f"\nJob Title: {job_title}")
    print(f"Description excerpt: {job_description[:80]}...")
    
    # Extract skills
    skills = extract_skills(job_title, job_description)
    print(f"\nExtracted Skills ({len(skills)} total):")
    for i, skill in enumerate(skills[:10], 1):
        print(f"  {i}. {skill}")
    
    # Check skill match
    print("\n" + "-"*40)
    print("Skill Matching Example:")
    
    user_skills = ["python", "machine learning", "aws", "react"]
    match_pct = skill_match_ratio(user_skills, skills)
    
    print(f"User skills: {user_skills}")
    print(f"Match rate: {match_pct:.1%}")


def example_url_canonicalization():
    """Example 3: Canonicalize URLs for deduplication."""
    print("\n" + "="*80)
    print("Example 3: URL Canonicalization for Dedup")
    print("="*80)
    
    urls = [
        "https://greenhouse.io/jobs/123?utm_source=linkedin&utm_medium=social",
        "https://GREENHOUSE.IO/jobs/123/?utm_source=indeed",
        "https://greenhouse.io/jobs/123",
        "https://greenhouse.io/jobs/456?utm_source=google",
    ]
    
    print(f"\nOriginal URLs:")
    for url in urls:
        print(f"  {url}")
    
    print(f"\nCanonical URLs:")
    for url in urls:
        canonical = canonicalize_url(url)
        hashed = url_hash(url)[:16]  # Show first 16 chars of hash
        print(f"  {canonical}")
        print(f"    → Hash: {hashed}...\n")
    
    # Check equivalence
    print("-"*40)
    print("URL Equivalence Checks:")
    
    for i, url1 in enumerate(urls[:2]):
        for j, url2 in enumerate(urls[2:], 2):
            equiv = are_urls_equivalent(url1, url2)
            result = "✓ Same job" if equiv else "✗ Different job"
            print(f"  URL {i} vs URL {j}: {result}")


def example_taxonomy():
    """Example 4: View and work with skill taxonomy."""
    print("\n" + "="*80)
    print("Example 4: Skill Taxonomy")
    print("="*80)
    
    taxonomy = get_taxonomy()
    
    print(f"\nTotal skills in taxonomy: {len(taxonomy)}")
    print(f"\nFirst 20 skills:")
    for skill in taxonomy[:20]:
        print(f"  - {skill}")
    
    print(f"\nSkills by category:")
    
    categories = {
        "Languages": ["python", "javascript", "java", "go"],
        "Cloud": ["aws", "azure", "gcp"],
        "Platforms": ["servicenow", "salesforce", "sap"],
        "ML/AI": ["machine learning", "nlp", "ai"],
    }
    
    for category, skills in categories.items():
        found = [s for s in skills if s in taxonomy]
        print(f"  {category}: {found}")


def main():
    """Run all examples."""
    print("\n" + "#"*80)
    print("# Submodules 3, 4, 5: Extraction (Skills, Salary, URLs) - Examples")
    print("#"*80)
    
    example_salary_extraction()
    example_skills_extraction()
    example_url_canonicalization()
    example_taxonomy()
    
    print("\n" + "#"*80)
    print("# Examples complete!")
    print("#"*80)


if __name__ == "__main__":
    main()
