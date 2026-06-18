"""Matching engine that scores a job against a user profile.

Uses skills, experience, job type/location and salary to compute score.
"""
from __future__ import annotations

from typing import List, Dict, Any
from ..extraction.skills_extractor import skill_match_ratio
from ..extraction.salary_parser import parse_salary
from .business_rules import get_weights
from .match_trace import MatchTrace


def score_job(job: Any, profile: Dict[str, Any]) -> MatchTrace:
    """Score a normalized job against a profile.

    profile should contain keys: required_skills, min_experience_years, preferred_locations, desired_job_types, desired_salary_min
    """
    weights = get_weights()
    trace = MatchTrace()

    # Stacks/skills
    required = profile.get("required_skills", [])
    stacks_ratio = skill_match_ratio(required, job.skills)
    trace.add("stacks", weights["stacks"], stacks_ratio)

    # Experience
    req_exp = profile.get("min_experience_years")
    job_exp = job.metadata.get("required_years") if hasattr(job, "metadata") else None
    exp_score = 1.0
    if req_exp and job_exp:
        exp_score = min(1.0, job_exp / req_exp)
    trace.add("experience", weights["experience"], exp_score)

    # Type and location
    type_score = 1.0 if job.job_type in profile.get("desired_job_types", [job.job_type]) else 0.0
    loc_ok = any(loc.lower() in job.location.lower() for loc in profile.get("preferred_locations", [job.location]))
    type_loc_score = 0.5 * type_score + 0.5 * (1.0 if loc_ok else 0.0)
    trace.add("type_location", weights["type_location"], type_loc_score)

    # Salary
    desired_min = profile.get("desired_salary_min")
    sal_min, sal_max, period = parse_salary(job.salary_text or job.description or "")
    salary_score = 0.0
    if desired_min and sal_min:
        salary_score = 1.0 if sal_min >= desired_min else sal_min / desired_min
    else:
        salary_score = 0.5  # unknown
    trace.add("salary", weights["salary"], salary_score)

    return trace
