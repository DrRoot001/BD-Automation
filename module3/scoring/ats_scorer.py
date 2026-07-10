"""ATS compatibility scoring engine using Gemini API."""
from __future__ import annotations

import os
import json
import asyncio
from typing import List, Optional
from pydantic import BaseModel, Field

from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import ResumeData

class ATSScore(BaseModel):
    overall: float = Field(description="Overall ATS score between 0 and 100")
    keyword_match: float = Field(description="Score between 0 and 100 for JD keywords found in the resume")
    skills_overlap: float = Field(description="Score between 0 and 100 for core technical skills overlap")
    experience_relevance: float = Field(description="Score between 0 and 100 for years and domain relevance")
    education_match: float = Field(description="Score of 0 or 100 indicating if degree requirements are met")
    formatting_score: float = Field(description="Score between 0 and 100 checking the layout/formatting friendliness")
    missing_keywords: List[str] = Field(default_factory=list, description="Keywords present in the JD but absent from the resume")

def _repair_json(text: str) -> str:
    """Best-effort repair of near-valid LLM JSON: missing commas between fields,
    trailing commas, and unclosed braces/brackets (truncated output)."""
    import re
    t = text.strip()
    # value/closing-brace followed by a quoted key on the next line, comma missing
    t = re.sub(r'([}\]"0-9truefalsnl])(\s*\n\s*")', r'\1,\2', t)
    # trailing commas before a closing brace/bracket
    t = re.sub(r',(\s*[}\]])', r'\1', t)
    # truncated output: close any dangling string, then balance brackets
    if t.count('"') % 2 == 1:
        t += '"'
    t += ']' * max(0, t.count('[') - t.count(']'))
    t += '}' * max(0, t.count('{') - t.count('}'))
    return t


def _heuristic_ats_score(resume: "ResumeData", job: NormalizedJob) -> ATSScore:
    """LLM-free fallback: token overlap between the JD keywords and the resume."""
    resume_blob = " ".join([
        resume.sections.summary or "",
        " ".join(resume.sections.skills or []),
        " ".join(b for exp in resume.sections.experience for b in (exp.bullets or [])),
    ]).lower()
    jd_keywords = [k for k in (job.skills or []) if k] or [
        w for w in set((job.description or "").lower().split()) if len(w) > 4
    ][:30]
    if not jd_keywords:
        matched, missing = [], []
        keyword_match = 50.0
    else:
        matched = [k for k in jd_keywords if k.lower() in resume_blob]
        missing = [k for k in jd_keywords if k.lower() not in resume_blob]
        keyword_match = 100.0 * len(matched) / len(jd_keywords)
    overall = min(100.0, 30.0 + 0.7 * keyword_match)
    return ATSScore(
        overall=overall,
        keyword_match=keyword_match,
        skills_overlap=keyword_match,
        experience_relevance=60.0,
        education_match=50.0,
        formatting_score=100.0,
        missing_keywords=missing[:15],
    )

class ScoringBreakdown(BaseModel):
    keyword_score_out_of_50: int = Field(description="Score out of 50 for keywords/skills matching")
    experience_score_out_of_30: int = Field(description="Score out of 30 for experience relevance")
    education_score_out_of_20: int = Field(description="Score out of 20 for education matching")
    formatting_penalty: int = Field(description="Formatting penalty, 0 or negative integer")

class LLMATSResponse(BaseModel):
    company_name: str = Field(description="Company name from the job description")
    job_title: str = Field(description="Job title from the job description")
    final_ats_score: int = Field(description="Overall ATS score between 0 and 100")
    scoring_breakdown: ScoringBreakdown = Field(description="Detailed breakdown of the ATS score")
    extracted_job_keywords: List[str] = Field(description="Key skills and keywords extracted from the job description")
    missing_keywords: List[str] = Field(description="Keywords required by the job description but missing or weak in the resume")
    brief_justification: str = Field(description="A short justification for the score")

_SYSTEM_PROMPT = """
You are an expert ATS (Applicant Tracking System) evaluator. Your job is to score a candidate's resume
against a provided job description. You must output ONLY a valid JSON object matching the requested schema.

Scoring rubric:
- keyword_score_out_of_50: How many required keywords/skills from the JD are present in the resume (0-50)
- experience_score_out_of_30: Relevance and depth of experience to the role (0-30)
- education_score_out_of_20: Education match to requirements (0-20)
- formatting_penalty: Deduct points for poor formatting, missing sections, etc. (0 or negative)
"""

async def calculate_ats_score(resume: ResumeData, job: NormalizedJob) -> ATSScore:
    """Calculate the ATS score of a resume against a job description using Gemini."""
    
    experience_text = "\n".join([
        f"- {exp.title} at {exp.company} ({exp.start_date} - {exp.end_date or 'Present'}): {exp.description} (Tech: {', '.join(exp.technologies)})"
        for exp in resume.sections.experience
    ])
    
    education_text = "\n".join([
        f"- {edu.degree} in {edu.field} from {edu.institution} ({edu.graduation_year or 'N/A'})"
        for edu in resume.sections.education
    ])

    job_data = {
        "company_name": job.company,
        "job_title": job.title,
        "description": job.description,
        "requirements": job.skills
    }

    resume_data_str = (
        f"Summary: {resume.sections.summary}\n"
        f"Skills: {', '.join(resume.sections.skills)}\n"
        f"Work Experience:\n{experience_text}\n"
        f"Education:\n{education_text}\n"
    )

    combined_prompt = (
        f"**Candidate Resume:**\n{resume_data_str}\n\n"
        f"**Job Details:**\n```json\n{json.dumps(job_data, indent=2)}\n```\n"
        "Please evaluate the attached resume."
    )

    from module3.utils.gemini import generate_content_with_retry

    result = None
    for attempt in range(2):
        try:
            response = await generate_content_with_retry(
                contents=combined_prompt,
                system_instruction=_SYSTEM_PROMPT,
                temperature=0.0,
                response_schema=LLMATSResponse
            )
        except Exception as llm_err:
            # Every provider is down (rate caps / no credits). Don't let a
            # scoring beauty-metric kill the application — go heuristic.
            print(f"ATS LLM call failed entirely ({str(llm_err)[:120]}) — using heuristic score.")
            break
        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            start = 1
            end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
            raw_text = "\n".join(lines[start:end]).strip()
            if raw_text.startswith("json"):
                raw_text = raw_text[4:].strip()
        try:
            result = json.loads(raw_text)
            if not isinstance(result, dict):
                raise ValueError(f"Parsed JSON is {type(result).__name__}, expected dictionary")
            break
        except Exception as first_err:
            try:
                repaired = json.loads(_repair_json(raw_text))
                if not isinstance(repaired, dict):
                    raise ValueError("repaired JSON is not a dictionary")
                result = repaired
                print(f"ATS response repaired after parse error: {first_err}")
                break
            except Exception:
                result = None
                print(f"Failed to parse LLM ATS evaluation response (attempt {attempt + 1}):", first_err)
                print("Raw response:", response.text)

    if result is None:
        # A broken beauty-metric response must not kill the whole application —
        # fall back to a keyword-overlap heuristic and keep the pipeline moving.
        print("ATS LLM scoring unusable after retry — using heuristic keyword-overlap score.")
        return _heuristic_ats_score(resume, job)

    breakdown = result.get("scoring_breakdown") or {}
    if not isinstance(breakdown, dict):
        breakdown = {}
    
    keyword_match = (breakdown.get("keyword_score_out_of_50", 0) / 50.0) * 100
    exp_rel = (breakdown.get("experience_score_out_of_30", 0) / 30.0) * 100
    edu_match = (breakdown.get("education_score_out_of_20", 0) / 20.0) * 100
    formatting = 100 + breakdown.get("formatting_penalty", 0)

    overall = result.get("final_ats_score", 0)
    
    return ATSScore(
        overall=float(overall),
        keyword_match=float(keyword_match),
        skills_overlap=float(keyword_match),
        experience_relevance=float(exp_rel),
        education_match=float(edu_match),
        formatting_score=float(formatting),
        missing_keywords=result.get("missing_keywords") or []
    )
