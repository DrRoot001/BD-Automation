"""ATS compatibility scoring engine using Gemini API."""
from __future__ import annotations

import os
import json
import re
import asyncio
from typing import List, Optional
from pydantic import BaseModel, Field


def _lenient_json_loads(raw: str):
    """Parse JSON that an LLM may have wrapped in fences or TRUNCATED mid-object.

    gemini-3.5-flash occasionally cuts a JSON response off before its closing
    brace, which crashes a strict json.loads. This strips code fences, trims to
    the outermost object/array, and balances any unclosed strings/brackets so a
    slightly-truncated-but-usable response still parses. Raises on genuinely
    unrecoverable input."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # Trim leading prose before the first JSON opener.
    idx = min([i for i in (s.find("{"), s.find("[")) if i != -1], default=-1)
    if idx > 0:
        s = s[idx:]
    s = re.sub(r",\s*$", "", s)  # drop a dangling trailing comma
    # Walk the text (ignoring string contents) to find unclosed brackets.
    closers, in_str, esc = [], False, False
    pairs = {"{": "}", "[": "]"}
    for ch in s:
        if esc:
            esc = False
            continue
        if in_str:
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in pairs:
            closers.append(pairs[ch])
        elif ch in ("}", "]") and closers and closers[-1] == ch:
            closers.pop()
    repaired = s + ('"' if in_str else "") + "".join(reversed(closers))
    return json.loads(repaired)

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

_SYSTEM_PROMPT = """
You are an expert ATS (Applicant Tracking System) evaluator. Your job is to score a candidate's resume
against a provided job description. You must output ONLY a valid JSON object with no markdown, no preamble.

Scoring rubric:
- keyword_score_out_of_50: How many required keywords/skills from the JD are present in the resume (0-50)
- experience_score_out_of_30: Relevance and depth of experience to the role (0-30)
- education_score_out_of_20: Education match to requirements (0-20)
- formatting_penalty: Deduct points for poor formatting, missing sections, etc. (0 or negative)

Output schema:
{
  "company_name": "<string>",
  "job_title": "<string>",
  "final_ats_score": <integer 0-100>,
  "scoring_breakdown": {
    "keyword_score_out_of_50": <integer>,
    "experience_score_out_of_30": <integer>,
    "education_score_out_of_20": <integer>,
    "formatting_penalty": <integer>
  },
  "extracted_job_keywords": ["<keyword>", ...],
  "missing_keywords": ["<keyword>", ...],
  "brief_justification": "<string>"
}
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

    # One full re-generation on unparseable JSON: the model occasionally emits
    # a glitched object (duplicated fragment after a closing quote — seen live
    # 2026-07-10) that even lenient parsing can't repair. A single fresh call
    # almost always succeeds; without it the whole job is skipped as llm_error.
    result = None
    last_err: Exception | None = None
    for parse_attempt in range(2):
        response = await generate_content_with_retry(
            contents=combined_prompt,
            system_instruction=_SYSTEM_PROMPT,
            temperature=0.0,
            response_mime_type="application/json"
        )
        try:
            raw_text = response.text.strip()
            if raw_text.startswith("```"):
                lines = raw_text.splitlines()
                start = 1
                end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
                raw_text = "\n".join(lines[start:end]).strip()

            result = _lenient_json_loads(raw_text)
            break
        except Exception as e:
            last_err = e
            print("Failed to parse LLM ATS evaluation response:", e)
            print("Raw response:", response.text)
            if parse_attempt == 0:
                print("[ATS] retrying LLM evaluation once (malformed JSON)")
    if result is None:
        raise ValueError(f"Failed to calculate ATS score: {last_err}")

    breakdown = result.get("scoring_breakdown", {})
    
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
        missing_keywords=result.get("missing_keywords", [])
    )
