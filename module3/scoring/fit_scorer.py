"""Candidate job fit scoring engine using Gemini API."""
from __future__ import annotations

import os
import json
import asyncio
from typing import List, Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import ResumeData
from module3.scoring.ats_scorer import calculate_ats_score

class MatchResult(BaseModel):
    job_id: str
    candidate_id: str
    fit_score: float              # 0-100
    ats_score: float              # 0-100
    combined_score: float         # (fit_score × 0.5) + (ats_score × 0.5)
    should_apply: bool            # combined_score >= 89
    matching_skills: List[str]    # skills found in both resume and JD
    missing_skills: List[str]     # skills in JD but not in resume
    experience_match: float       # 0-100, years + domain relevance
    reasoning: str                # LLM explanation of score

async def score_job_fit(
    candidate: dict,
    resume: ResumeData,
    job: NormalizedJob
) -> MatchResult:
    """Evaluate job fit using the ATS score only, as fit scoring has been removed."""
    # Fetch ATS score
    ats_score_obj = await calculate_ats_score(resume, job)
    ats_score = ats_score_obj.overall

    # Fit score and combined score are both equal to the ATS score since fit scoring is removed
    fit_score = ats_score
    combined_score = ats_score

    try:
        _apply_threshold = float(os.getenv("APPLY_SCORE_THRESHOLD", "89"))
    except ValueError:
        _apply_threshold = 89.0
    should_apply = combined_score >= _apply_threshold

    return MatchResult(
        job_id=str(job.job_id) if job.job_id else "job-unknown",
        candidate_id=str(candidate.get("id")) if candidate.get("id") else "candidate-unknown",
        fit_score=fit_score,
        ats_score=ats_score,
        combined_score=combined_score,
        should_apply=should_apply,
        matching_skills=[],  # Fit scoring removed
        missing_skills=ats_score_obj.missing_keywords,
        experience_match=ats_score_obj.experience_relevance,
        reasoning=f"ATS score is {ats_score}."
    )

