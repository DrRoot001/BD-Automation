# Submodules 3, 4, 5: Extraction Engine (Skills, Salary, URLs)

## Overview

Extraction submodules parse raw text from job descriptions and metadata to extract structured data fields:

- **Submodule 3**: Skills extraction from title and description
- **Submodule 4**: Salary parsing from text (multiple formats)
- **Submodule 5**: URL canonicalization for deduplication

## Architecture

```
RawJobData (from adapters)
    ↓
Extraction Engine
    ├─ Skills Extractor (Submodule 3)
    ├─ Salary Parser (Submodule 4)
    └─ URL Canonicalizer (Submodule 5)
    ↓
NormalizedJob (unified schema)
```

## Submodule 3: Skills Extraction

### Purpose
Extract technical skills from job title and description using a predefined taxonomy.

### File: `skills_extractor.py`

### Functions

#### `extract_skills(title: str, description: str) -> List[str]`

Extracts skills using regex pattern matching against SKILL_TAXONOMY.

**Features:**
- Word boundary matching (avoids partial matches like "go" matching "golang")
- Sorted by frequency (most mentioned skills first)
- Case-insensitive matching
- Extensible taxonomy

**Example:**
```python
from module2.extraction import extract_skills

skills = extract_skills(
    title="ML Engineer",
    description="Python, TensorFlow, AWS expertise"
)
# → ["python", "tensorflow", "aws"]
```

#### `skill_match_ratio(required_skills: List[str], job_skills: List[str]) -> float`

Calculate percentage of required skills found in job.

**Example:**
```python
required = ["python", "aws", "docker"]
job_skills = ["python", "aws", "kubernetes"]

ratio = skill_match_ratio(required, job_skills)
# → 0.667 (2 of 3 found)
```

#### `get_taxonomy() -> List[str]`

Returns the current skill taxonomy (all recognized skills).

### Skill Taxonomy

The taxonomy includes:
- **ML/AI**: machine learning, deep learning, nlp, computer vision, ai
- **Languages**: python, javascript, java, go, rust, kotlin, etc.
- **Platforms**: aws, azure, gcp, servicenow, salesforce, sap
- **Frontend**: react, vue, angular, nextjs, typescript
- **Backend**: nodejs, django, flask, spring, fastapi
- **DevOps**: docker, kubernetes, terraform, jenkins, ci/cd
- **Databases**: postgresql, mongodb, redis, elasticsearch, nosql
- **Soft Skills**: communication, leadership, teamwork, problem solving

### Extending the Taxonomy

```python
from module2.extraction import add_skill

add_skill("webassembly")
add_skill("dbt")
```

## Submodule 4: Salary Parser

### Purpose
Parse various salary text formats and extract min/max values and pay period.

### File: `salary_parser.py`

### Functions

#### `parse_salary(salary_text: Optional[str]) -> Tuple[Optional[int], Optional[int], str]`

Parse salary text and return (min, max, period).

**Handles:**
- Range formats: "$120k - $150k"
- Hourly: "$60/hour", "$60 per hour"
- Annual: "$80,000 - $120,000 per year"
- Single values: "$120k", "$80,000"
- 'k' suffix: "120k" → 120000
- Missing/invalid: Returns (None, None, "yearly")

**Examples:**
```python
from module2.extraction import parse_salary

parse_salary("$60/hour")
# → (60, None, "hourly")

parse_salary("$120k - $150k")
# → (120000, 150000, "yearly")

parse_salary("$80,000 - $120,000 per year")
# → (80000, 120000, "yearly")

parse_salary("competitive")
# → (None, None, "yearly")
```

#### `format_salary(salary_min: Optional[int], salary_max: Optional[int], pay_period: str) -> str`

Format parsed salary for display.

**Examples:**
```python
from module2.extraction import format_salary

format_salary(60, None, "hourly")
# → "$60/hour"

format_salary(120000, 150000, "yearly")
# → "$120k - $150k/year"
```

### Parsing Algorithm

1. Detect pay period (hourly vs yearly) from keywords
2. Extract all currency amounts using regex patterns
3. Handle 'k' suffix multiplication
4. Sort amounts and return min/max
5. Return (None, None, period) if no amounts found

## Submodule 5: URL Canonicalization

### Purpose
Normalize URLs by removing tracking parameters for accurate deduplication.

### File: `url_canonicalizer.py`

### Functions

#### `canonicalize_url(url: str) -> str`

Normalize URL: strip tracking params, lowercase, remove fragments.

**Features:**
- Removes scheme/domain casing
- Removes trailing slashes in paths
- Removes known tracking parameters (utm_*, gclid, fbclid, etc.)
- Preserves functional query parameters (e.g., page=1)
- Removes URL fragments (#)

**Removed Parameters:**
- Google Analytics: utm_source, utm_medium, utm_campaign, utm_content, utm_term
- Tracking pixels: gclid, fbclid, msclkid, mc_cid, mc_eid
- Generic: ref, source

**Examples:**
```python
from module2.extraction import canonicalize_url

canonicalize_url("https://example.com/jobs/123?utm_source=linkedin")
# → "https://example.com/jobs/123"

canonicalize_url("https://EXAMPLE.COM/jobs/123/?utm_source=google")
# → "https://example.com/jobs/123"

canonicalize_url("https://example.com/jobs/123?page=1&utm_source=ref")
# → "https://example.com/jobs/123?page=1"
```

#### `url_hash(url: str) -> str`

Generate SHA256 hash of canonical URL for fast Redis lookup.

**Example:**
```python
from module2.extraction import url_hash

hash1 = url_hash("https://example.com/jobs/123?utm_source=linkedin")
hash2 = url_hash("https://example.com/jobs/123?utm_source=google")

assert hash1 == hash2  # Same canonical URL
```

#### `are_urls_equivalent(url1: str, url2: str) -> bool`

Check if two URLs are equivalent (same canonical form).

**Example:**
```python
from module2.extraction import are_urls_equivalent

are_urls_equivalent(
    "https://example.com/jobs/123?utm_source=linkedin",
    "https://EXAMPLE.COM/jobs/123/?utm_source=google"
)
# → True (same canonical)
```

### Use in Deduplication

Layer 1 (URL-based dedup) uses canonicalization:

```python
# Check if job URL already exists
canonical_url = canonicalize_url(raw_job.url)
hash_value = url_hash(raw_job.url)

# Fast Redis lookup by hash
existing = redis.get(f"job_hash:{hash_value}")

if existing:
    # Duplicate detected
    return None
else:
    # New job, proceed to Layer 2
    redis.set(f"job_hash:{hash_value}", raw_job_id)
```

## Integration with Module 2 Normalization

The Normalizer uses extraction submodules:

```python
from module2.normalization import normalize
from module2.adapters import RawJobData

raw_job = RawJobData(...)

normalized = normalize(raw_job)
# Internally calls:
#   - extract_skills(title, description)
#   - parse_salary(salary_text)
#   - canonicalize_url(url)
```

### Normalized Job Result

```python
NormalizedJob(
    title="Senior Python Engineer",
    company="TechCorp",
    location="Remote, USA",
    url="https://techcorp.com/jobs/123",
    description="...",
    skills=["python", "aws", "docker"],      # From Submodule 3
    salary_min=120000,                        # From Submodule 4
    salary_max=150000,
    pay_period="yearly",
    job_type="full-time",
    canonical_url="https://techcorp.com/jobs/123",  # From Submodule 5
    posted_at=datetime(...),
    source="greenhouse",
    embedding=None,  # Set in Submodule 6
)
```

## Testing

### Quick Tests
```bash
cd module2/extraction
python test_extraction_quick.py
```

### Expected Output
```
✓ Test Salary Parser
  ✓ '$60/hour' → (60, None, 'hourly')
  ✓ '$80,000 - $120,000 per year' → (80000, 120000, 'yearly')
  ...

✓ Test Skills Extraction
  ✓ 'Python Developer' → ['python', ...]

✓ Test URL Canonicalization
  ✓ Canonical: ...https://example.com/jobs/123

✓ All extraction tests passed!
```

## Examples

Run example scripts:

```bash
# Extraction examples
python module2/extraction/example_usage.py

# Full normalization with extraction
python module2/normalization/example_usage.py
```

## Performance Notes

- **Skills extraction**: O(n) where n = description length, matching 200+ skills
- **Salary parsing**: O(1) regex patterns
- **URL canonicalization**: O(1) URL parsing and parameter filtering
- **Batch processing**: All functions support list operations for parallelization

## Dependencies

All submodules use Python standard library only:
- `re`: regex pattern matching (skills, salary, URLs)
- `urllib.parse`: URL parsing
- `hashlib`: SHA256 hashing
- `datetime`: date handling (indirectly via RawJobData)

No external dependencies required!

## Next Steps

These extraction modules feed into:

1. **Submodule 2 (Normalization)**: Uses all three extraction modules
2. **Submodule 6 (Embedding)**: Uses extracted skills for context
3. **Submodule 7 (Deduplication)**: Uses URL hashing for Layer 1 dedup
4. **Submodule 8 (Matching)**: Uses extracted salary and skills for scoring

## Related Files

- `module2/normalization/normalizer.py`: Orchestrates extraction
- `module2/normalization/schemas.py`: NormalizedJob dataclass
- `module2/deduplication/`: Uses canonicalization and hashing
