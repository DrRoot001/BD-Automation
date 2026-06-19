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

    import google.genai.types as genai_types
    from google import genai
    
    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: client.models.generate_content(
            model="gemini-2.5-flash",
            contents=combined_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                temperature=0.0, 
            ),
        )
    )

    try:
        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            start = 1
            end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
            raw_text = "\n".join(lines[start:end]).strip()
        
        result = json.loads(raw_text)
    except Exception as e:
        print("Failed to parse LLM ATS evaluation response:", e)
        print("Raw response:", response.text)
        raise ValueError(f"Failed to calculate ATS score: {e}")

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
