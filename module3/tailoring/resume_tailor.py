"""Resume tailoring engine using the Fabricator Agent loop and Gemini."""
from __future__ import annotations

import os
import json
import asyncio
import re
import logging
from typing import List
from pydantic import BaseModel, Field
from google import genai
from google.genai import types as genai_types

from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import ResumeData, ExperienceEntry, EducationEntry
from module3.scoring.ats_scorer import calculate_ats_score
from module3.tailoring.pdf_generator import generate_resume_pdf
from module3.utils.storage import safe_filename

logger = logging.getLogger("resume_tailor")

class TailoredResume(BaseModel):
    candidate_id: str
    job_id: str
    version: int
    original_resume_id: str
    modified_summary: str
    modified_skills: List[str]
    modified_keywords: List[str]
    experience: List[ExperienceEntry]
    education: List[EducationEntry]
    pdf_url: str
    ats_score_before: float
    ats_score_after: float

_FABRICATOR_SYSTEM = """
You are a master Resume ATS Optimizer and Senior Technical Recruiter operating in the year 2026.
Your task is to take a candidate's Resume JSON, analyze the ATS Feedback, and heavily enhance the JSON to score ABOVE 90 against the Target Job Description.

You MUST follow these STRICT GUARDRAILS. Violating them is FORBIDDEN:

1. ZERO MARKDOWN (CRITICAL):
   - ABSOLUTELY NO MARKDOWN. Do not use asterisks (**), italics, or bolding anywhere inside the JSON text strings. The text must be clean and plain.

2. HEADLINE & CONTACT INFO:
   - HEADLINE: You MUST generate a professional headline (e.g., "Senior Machine Learning Engineer | Generative AI") under `basics.headline` that matches the Target Job Description.
   - LOCATION: You MUST ensure the location reflects a USA residence. If it is outside the USA, change it to a suitable US tech hub.
   - LINKS: You MUST keep all original links (LinkedIn, GitHub, Portfolio) EXACTLY as they are.

3. SUMMARY & SKILLS:
   - SUMMARY: Edit and rewrite the summary block to naturally weave in missing ATS keywords to guarantee a high score.
   - SKILLS: You may ADD missing ATS keywords to the skills section, but do not erase the candidate's core baseline skills.

4. EXPERIENCE (STRICT NO-FABRICATION RULE):
   - COMPANIES & DATES: You MUST keep the exact company names and dates as listed in the original resume. DO NOT invent new companies.
   - PRESENT ROLE: You are permitted to change the TITLE of the most recent/present role to better align with the target job.
   - BULLET POINTS: You MUST enhance the descriptions of both present and past roles using the XYZ/STAR method. You must weave missing ATS keywords NATURALLY into these sentences.

5. PROJECTS:
   - If projects are provided, enhance their descriptions and listed technologies so they heavily match the job description and requirements.

6. EDUCATION & CERTIFICATIONS (STRICT NO-FABRICATION RULE):
   - EDUCATION: You MUST keep the candidate's exact degree, major, university, descriptions, and dates as listed in the original resume. DO NOT alter, add, or fabricate any educational details.
   - CERTIFICATIONS: DO NOT invent or add new certifications. If certifications exist in the original resume, you MUST retain them EXACTLY as they are with their original dates and descriptions.

7. OUTPUT SCHEMA (CRITICAL):
   - Return ONLY raw, valid JSON. DO NOT wrap in ```json blocks.
   - Schema must include "basics", "summary", "skills", "experience", "education", "projects", and "certifications".
   - "basics" must contain: "name", "headline", "email", "phone", "location", "linkedin", "github_portfolio".
"""

async def tailor_resume(
    resume: ResumeData,
    job: NormalizedJob,
    candidate_profile: dict,
    output_pdf_dir: str = "backend/data/tailored_resumes",
    version: int = 1
) -> TailoredResume:
    """Tailor a candidate's resume by looping through the Fabricator Agent."""
    
    ats_score_before_obj = await calculate_ats_score(resume, job)
    ats_score_before = ats_score_before_obj.overall
    
    current_ats_score = ats_score_before
    current_missing_keywords = ats_score_before_obj.missing_keywords
    
    resume_json = {
        "basics": {
            "name": candidate_profile.get("name", "Candidate"),
            "headline": "",
            "email": candidate_profile.get("email", ""),
            "phone": candidate_profile.get("phone", ""),
            "location": candidate_profile.get("location", ""),
            "linkedin": candidate_profile.get("linkedin_url", ""),
            "github_portfolio": ""
        },
        "summary": resume.sections.summary,
        "skills": [{"category": "General", "keywords": resume.sections.skills}],
        "experience": [
            {
                "company": exp.company,
                "title": exp.title,
                "location": None,
                "date": f"{exp.start_date} - {exp.end_date or 'Present'}",
                "technologies_used": exp.technologies,
                "bullets": [exp.description]
            } for exp in resume.sections.experience
        ],
        "education": [
            {
                "institution": edu.institution,
                "degree": edu.degree,
                "date": str(edu.graduation_year) if edu.graduation_year else None
            } for edu in resume.sections.education
        ],
        "projects": [],
        "certifications": resume.sections.certifications
    }

    job_data = {
        "company_name": job.company,
        "job_title": job.title,
        "description": job.description,
        "requirements": job.skills
    }

    from module3.utils.gemini import generate_content_with_retry

    max_loops = 5
    loop_count = 0
    final_resume_json = resume_json

    while current_ats_score < 90.0 and loop_count < max_loops:
        logger.info(f"Fabrication Loop {loop_count + 1}/{max_loops} - Current ATS: {current_ats_score}")
        
        user_prompt = (
            f"SYSTEM INSTRUCTION:\n{_FABRICATOR_SYSTEM}\n\n"
            f"**ATS Feedback:**\nCurrent Score: {current_ats_score}/100\nMissing Keywords: {', '.join(current_missing_keywords)}\n\n"
            f"**Candidate Resume:**\n```json\n{json.dumps(final_resume_json, indent=2)}\n```\n\n"
            f"**Job Details:**\n```json\n{json.dumps(job_data, indent=2)}\n```\n"
        )
        
        try:
            response = await generate_content_with_retry(
                contents=user_prompt,
                temperature=0.3,
                response_mime_type="application/json"
            )
            
            raw_text = response.text.strip()
            if raw_text.startswith("```"):
                lines = raw_text.splitlines()
                start = 1
                end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
                raw_text = "\n".join(lines[start:end]).strip()
                
            final_resume_json = json.loads(raw_text)
            
        except Exception as e:
            logger.error(f"Fabrication failed on loop {loop_count}: {e}")
            break

        # Convert back to internal ResumeData to score again
        temp_skills = []
        for sg in final_resume_json.get("skills", []):
            temp_skills.extend(sg.get("keywords", []))
            
        temp_exp = []
        for exp in final_resume_json.get("experience", []):
            date_str = exp.get("date", "")
            parts = date_str.split("-")
            temp_exp.append(ExperienceEntry(
                company=exp.get("company", ""),
                title=exp.get("title", ""),
                start_date=parts[0].strip() if len(parts) > 0 else "",
                end_date=parts[1].strip() if len(parts) > 1 else "Present",
                description=" ".join(exp.get("bullets", [])),
                technologies=exp.get("technologies_used", [])
            ))
            
        temp_edu = []
        for edu in final_resume_json.get("education", []):
            date_str = edu.get("date", "")
            digits = re.findall(r'\d{4}', str(date_str))
            temp_edu.append(EducationEntry(
                institution=edu.get("institution", ""),
                degree=edu.get("degree", ""),
                field="",
                graduation_year=int(digits[0]) if digits else None
            ))
            
        temp_resume_data = resume.model_copy(deep=True)
        temp_resume_data.sections.summary = final_resume_json.get("summary", "")
        temp_resume_data.sections.skills = temp_skills
        temp_resume_data.sections.experience = temp_exp
        temp_resume_data.sections.education = temp_edu
        temp_resume_data.sections.certifications = final_resume_json.get("certifications", [])
        
        new_ats_obj = await calculate_ats_score(temp_resume_data, job)
        current_ats_score = new_ats_obj.overall
        current_missing_keywords = new_ats_obj.missing_keywords
        loop_count += 1

    flat_skills = []
    for sg in final_resume_json.get("skills", []):
        flat_skills.extend(sg.get("keywords", []))
        
    final_experience = []
    pdf_experience = []
    for exp in final_resume_json.get("experience", []):
        date_str = exp.get("date", "")
        parts = date_str.split("-")
        final_experience.append(ExperienceEntry(
            company=exp.get("company", "Company"),
            title=exp.get("title", "Position"),
            start_date=parts[0].strip() if len(parts) > 0 else "",
            end_date=parts[1].strip() if len(parts) > 1 else "Present",
            description=" ".join(exp.get("bullets", [])),
            technologies=exp.get("technologies_used", [])
        ))
        pdf_experience.append({
            "company": exp.get("company", "Company"),
            "title": exp.get("title", "Position"),
            "location": exp.get("location", ""),
            "date": exp.get("date", ""),
            "technologies_used": exp.get("technologies_used", []),
            "bullets": exp.get("bullets", [])
        })

    final_education = []
    pdf_education = []
    for edu in final_resume_json.get("education", []):
        date_str = edu.get("date", "")
        digits = re.findall(r'\d{4}', str(date_str))
        final_education.append(EducationEntry(
            institution=edu.get("institution", "Institution"),
            degree=edu.get("degree", ""),
            field="",
            graduation_year=int(digits[0]) if digits else None
        ))
        pdf_education.append({
            "institution": edu.get("institution", "Institution"),
            "degree": edu.get("degree", ""),
            "date": edu.get("date", "")
        })

    basics = final_resume_json.get("basics", {})
    candidate_name = basics.get("name", "Candidate")
    pdf_filename = f"{safe_filename(candidate_name, default='candidate', extension='')}_resume_v{version}.pdf"
    output_pdf_path = os.path.join(output_pdf_dir, pdf_filename)
    pdf_projects = final_resume_json.get("projects", [])

    # Wrap the synchronous rendering in asyncio.to_thread to prevent event loop blocking
    await asyncio.to_thread(
        generate_resume_pdf,
        output_path=output_pdf_path,
        name=basics.get("name", "Candidate"),
        headline=basics.get("headline", ""),
        email=basics.get("email", ""),
        phone=basics.get("phone", ""),
        location=basics.get("location", ""),
        linkedin_url=basics.get("linkedin", ""),
        summary=final_resume_json.get("summary", ""),
        skills=flat_skills,
        experience=pdf_experience,
        education=pdf_education,
        certifications=final_resume_json.get("certifications", []),
        projects=pdf_projects  # Used for rendering, discarded from return payload
    )

    return TailoredResume(
        candidate_id=resume.candidate_id or "candidate-unknown",
        job_id=job.job_id or "job-unknown",
        version=version,
        original_resume_id=resume.resume_id or "resume-unknown",
        modified_summary=final_resume_json.get("summary", ""),
        modified_skills=flat_skills,
        modified_keywords=flat_skills,
        experience=final_experience,
        education=final_education,
        pdf_url=output_pdf_path,
        ats_score_before=ats_score_before,
        ats_score_after=current_ats_score
    )