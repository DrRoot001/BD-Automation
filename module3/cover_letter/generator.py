"""Cover letter generation engine using Gemini API."""
from __future__ import annotations

import os
import json
import asyncio
import logging
from pydantic import BaseModel
from google import genai
from google.genai import types as genai_types

from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import ResumeData
from module3.tailoring.pdf_generator import generate_cover_letter_pdf
from module3.utils.storage import safe_filename

logger = logging.getLogger("cover_letter_generator")

def _first_non_blank(*values) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""

class CoverLetter(BaseModel):
    candidate_id: str
    job_id: str
    content: str
    pdf_url: str

_COVER_LETTER_SYSTEM = """
You are an expert career coach who writes sharp, impactful cover letters.
Given a resume JSON and a job description, write a professional cover letter.

Output ONLY a valid JSON object — no markdown, no preamble — in this schema:
{
  "paragraphs": [
    "<One single, combined paragraph containing your entire message. DO NOT output multiple paragraphs.>"
  ]
}

STRICT RULES:
1. ONE PARAGRAPH ONLY: You MUST output exactly ONE string inside the "paragraphs" array. Combine your opening, achievements, and call to action into a single cohesive block of text.
2. WORD COUNT (CRITICAL): The total word count of this single paragraph MUST be strictly less than 150 words. Count carefully before outputting.
3. Content: State your expertise, integrate 2-3 specific numeric achievements from the resume that map to the job, and end with a strong call to action.
4. Tone: Confident, direct, zero filler phrases. Mirror keywords from the job description naturally.
"""

async def generate_cover_letter(
    resume: ResumeData,
    job: NormalizedJob,
    candidate_profile: dict,
    output_pdf_dir: str = "backend/data/cover_letters"
) -> CoverLetter:
    """Generate a highly tailored, concise cover letter PDF using JSON structured output."""
    
    experience_text = "\n".join([
        f"- {exp.title} at {exp.company} ({exp.start_date} - {exp.end_date or 'Present'}): {exp.description} (Tech: {', '.join(exp.technologies)})"
        for exp in resume.sections.experience
    ])
    
    user_prompt = (
        f"--- JOB DETAILS ---\n"
        f"Role Title: {job.title}\n"
        f"Company Name: {job.company}\n"
        f"Description:\n{job.description}\n\n"
        f"--- CANDIDATE DETAILS ---\n"
        f"Candidate Name: {candidate_profile.get('name', 'Candidate')}\n"
        f"Work Experience History:\n{experience_text}\n"
    )

    from module3.utils.gemini import generate_content_with_retry
    
    try:
        response = await generate_content_with_retry(
            contents=user_prompt,
            system_instruction=_COVER_LETTER_SYSTEM,
            temperature=0.2,
            response_mime_type="application/json"
        )
        
        raw_text = response.text.strip()
        
        # Strip markdown fences if the LLM accidentally includes them
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            start = 1
            end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
            raw_text = "\n".join(lines[start:end]).strip()
            
        cl_json = json.loads(raw_text)
        
    except Exception as e:
        logger.error(f"Failed to generate cover letter JSON: {e}")
        # Safe fallback in case of rate limit or JSON schema failure
        cl_json = {
            "paragraphs": [
                "I am writing to express my strong interest in the open position. My technical background aligns well with the core requirements outlined in the job description, and I would welcome the opportunity to discuss how I can leverage my experience to contribute to your engineering team. Thank you for your time and consideration."
            ]
        }

    # Extract the paragraphs for the PDF generator. The LLM's JSON shape varies
    # by model/run: gemini-3.5-flash sometimes returns a BARE LIST of paragraph
    # strings, other times a dict keyed "paragraphs"/"cover_letter"/"body", or a
    # single string. The old code assumed a dict and crashed with
    # "'list' object has no attribute 'get'". Normalize every shape here.
    def _coerce_paragraphs(obj) -> list:
        if isinstance(obj, list):
            return [str(p).strip() for p in obj if str(p).strip()]
        if isinstance(obj, dict):
            val = (obj.get("paragraphs") or obj.get("cover_letter")
                   or obj.get("body") or obj.get("content") or [])
            if isinstance(val, str):
                return [s.strip() for s in val.split("\n\n") if s.strip()]
            if isinstance(val, list):
                return [str(p).strip() for p in val if str(p).strip()]
            return []
        if isinstance(obj, str):
            return [s.strip() for s in obj.split("\n\n") if s.strip()]
        return []

    letter_paragraphs = _coerce_paragraphs(cl_json)
    if not letter_paragraphs:
        # Never emit an empty cover letter — fall back to a generic body.
        letter_paragraphs = [
            "I am writing to express my strong interest in the open position. My "
            "technical background aligns well with the core requirements outlined "
            "in the job description, and I would welcome the opportunity to discuss "
            "how I can contribute to your team. Thank you for your time and consideration."
        ]
    letter_content = "\n\n".join(letter_paragraphs)
    
    candidate_name = _first_non_blank(candidate_profile.get("name"), "Candidate")
    pdf_filename = f"{safe_filename(candidate_name, default='candidate', extension='')}_cover_letter.pdf"
    output_pdf_path = os.path.join(output_pdf_dir, pdf_filename)
    
    candidate_location = _first_non_blank(candidate_profile.get("location"), "US")
    candidate_email = _first_non_blank(candidate_profile.get("email"), getattr(resume.sections, "email", None), "email@example.com")
    candidate_phone = _first_non_blank(candidate_profile.get("phone"), getattr(resume.sections, "phone", None))
    
    candidate_info = f"{candidate_location}  |  {candidate_email}"
    if candidate_phone:
        candidate_info += f"  |  {candidate_phone}"
        
    print(f"Compiling cover letter PDF to: {output_pdf_path}...")
    
    # Asynchronous non-blocking generation using xhtml2pdf
    await asyncio.to_thread(
        generate_cover_letter_pdf,
        output_path=output_pdf_path,
        candidate_name=candidate_name,
        candidate_info=candidate_info,
        company_name=job.company,
        job_title=job.title,
        letter_text=letter_content
    )
    
    return CoverLetter(
        candidate_id=resume.candidate_id or "candidate-unknown",
        job_id=job.job_id or "job-unknown",
        content=letter_content,
        pdf_url=output_pdf_path
    )
