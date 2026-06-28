import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from module2.normalization.schemas import normalize_job
from module2.run_scrape import map_item_to_job_create


SAMPLE_ITEM = {
    "title": "Senior Machine Learning Engineer",
    "company": "OpenAI",
    "location": "Remote",
    "source_url": "https://example.com/jobs/123",
    "canonical_url": "https://example.com/jobs/123",
    "description": "Build ML systems and deploy models at scale.",
    "skills": "Python, PyTorch, Kubernetes",
    "salary_min": "180000",
    "salary_max": "240000",
    "pay_period": "yearly",
    "job_type": "full time",
    "posted_at": "2026-06-28",
}


def main() -> None:
    mapped = map_item_to_job_create(SAMPLE_ITEM, "https://example.com/jobs")
    print("Mapped job output:")
    print(json.dumps(mapped, indent=2))

    if mapped:
        normalized = normalize_job(mapped)
        print("\nNormalized job output:")
        print(json.dumps(normalized.model_dump(), indent=2, default=str))


if __name__ == "__main__":
    main()
