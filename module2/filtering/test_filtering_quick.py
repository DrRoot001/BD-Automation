"""Quick tests for filtering and matching engine."""
from __future__ import annotations

import sys
from pathlib import Path
# add project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from module2.filtering.matcher import score_job
from types import SimpleNamespace


def main():
    job = SimpleNamespace(
        skills=["python", "aws"],
        job_type="full-time",
        location="Remote",
        salary_text="$120k - $150k",
        description="",
        metadata={"required_years": 3},
    )
    profile = {
        "required_skills": ["python", "aws"],
        "min_experience_years": 2,
        "preferred_locations": ["Remote"],
        "desired_job_types": ["full-time"],
        "desired_salary_min": 100000,
    }
    trace = score_job(job, profile)
    print("Score:", trace.score)
    assert trace.score > 0.5
    print("✓ Filtering score computed")

if __name__ == "__main__":
    main()
