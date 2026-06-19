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
    should_apply: bool            # combined_score >= 70
    matching_skills: List[str]    # skills found in both resume and JD
    missing_skills: List[str]     # skills in JD but not in resume
    experience_match: float       # 0-100, years + domain relevance
    reasoning: str                # LLM explanation of score

class LLMFitEvaluation(BaseModel):
    skills_overlap: float = Field(description="Score between 0 and 100 for technical skills overlap")
    experience_relevance: float = Field(description="Score between 0 and 100 for work history domain relevance")
    location_match: float = Field(description="Score: 100 if location matches Remote/US requirements, 0 otherwise")
    seniority_match: float = Field(description="Score between 0 and 100 matching the seniority level of JD and candidate")
    matching_skills: List[str]
    missing_skills: List[str]
    reasoning: str

async def score_job_fit(
    candidate: dict,
    resume: ResumeData,
    job: NormalizedJob
) -> MatchResult:
    """Evaluate job fit between candidate profile, resume, and normalized job description."""
    # Using central Gemini retry wrapper
    
    # Format candidate details
    candidate_tech_stack = ", ".join(candidate.get("tech_stack", []))
    candidate_years_exp = candidate.get("years_exp", 0)
    candidate_location = candidate.get("location", "US")
    candidate_work_auth = candidate.get("work_auth", "us_authorized")
    
    experience_text = "\n".join([
        f"- {exp.title} at {exp.company} ({exp.start_date} - {exp.end_date or 'Present'}): {exp.description} (Tech: {', '.join(exp.technologies)})"
        for exp in resume.sections.experience
    ])

    prompt = (
        "You are an expert recruitment matching system. Analyze the candidate profile and resume "
        "against the provided Job Description (JD) and evaluate the following dimensions:\n\n"
        
        "1. skills_overlap: Technical skills required in JD versus candidate profile tech stack and resume.\n"
        "2. experience_relevance: Candidate years of experience and work history relevance to JD requirements.\n"
        "3. location_match: Check candidate location and work authorization versus JD. Score as 100 if candidate is Remote or in the required region, otherwise 0.\n"
        "4. seniority_match: Check if candidate matches JD required seniority (Junior, Mid, Senior, Lead). Seniority matching score between 0 and 100.\n"
        "5. matching_skills: List technical skills in JD that candidate possesses.\n"
        "6. missing_skills: List technical skills in JD that candidate is missing.\n"
        "7. reasoning: Return a detailed 2-3 sentence professional hiring explanation of the fit score.\n\n"
        
        f"--- JOB DESCRIPTION ---\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location}\n"
        f"Description:\n{job.description}\n"
        f"Skills Required: {', '.join(job.skills)}\n\n"
        
        f"--- CANDIDATE PROFILE ---\n"
        f"Tech Stack: {candidate_tech_stack}\n"
        f"Years Experience: {candidate_years_exp}\n"
        f"Location: {candidate_location}\n"
        f"Work Authorization: {candidate_work_auth}\n\n"
        
        f"--- CANDIDATE RESUME ---\n"
        f"Summary: {resume.sections.summary}\n"
        f"Skills: {', '.join(resume.sections.skills)}\n"
        f"Work Experience:\n{experience_text}\n"
    )

    from module3.utils.gemini import generate_content_with_retry
    response = await generate_content_with_retry(
        contents=prompt,
        response_schema=LLMFitEvaluation,
        temperature=0.1
    )

    try:
        eval_data = json.loads(response.text)
        result = LLMFitEvaluation(**eval_data)
    except Exception as e:
        print("Failed to parse LLM Job Fit evaluation response:", e)
        print("Raw response:", response.text)
        raise ValueError(f"Failed to calculate job fit score: {e}")

    # Calculate fit_score using specified weights:
    # skills_overlap: 40%
    # experience_relevance: 35%
    # location_match: 15%
    # seniority_match: 10%
    fit_score = (
        (result.skills_overlap * 0.40) +
        (result.experience_relevance * 0.35) +
        (result.location_match * 0.15) +
        (result.seniority_match * 0.10)
    )
    fit_score = round(max(0.0, min(100.0, fit_score)), 2)

    # Fetch ATS score
    ats_score_obj = await calculate_ats_score(resume, job)
    ats_score = ats_score_obj.overall

    # Combined score is average of fit and ATS
    combined_score = round((fit_score * 0.5) + (ats_score * 0.5), 2)
    should_apply = combined_score >= 70.0

    return MatchResult(
        job_id=job.job_id or "job-unknown",
        candidate_id=candidate.get("id") or "candidate-unknown",
        fit_score=fit_score,
        ats_score=ats_score,
        combined_score=combined_score,
        should_apply=should_apply,
        matching_skills=result.matching_skills,
        missing_skills=result.missing_skills,
        experience_match=round(result.experience_relevance, 2),
        reasoning=result.reasoning
    )
