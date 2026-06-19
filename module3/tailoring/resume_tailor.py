"""Resume tailoring engine using Gemini and validation checks."""
from __future__ import annotations

import os
import json
import asyncio
from typing import List, Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import ResumeData, ExperienceEntry, EducationEntry
from module3.scoring.ats_scorer import calculate_ats_score
from module3.tailoring.pdf_generator import generate_resume_pdf

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

class LLMTailoringOutput(BaseModel):
    modified_summary: str = Field(description="Rewritten summary highlighting JD-relevant skills")
    modified_skills: List[str] = Field(description="Reordered + optimized list of skills based strictly on candidate qualifications and JD")
    modified_keywords: List[str] = Field(description="Core keyword list optimized for ATS systems")

async def tailor_resume(
    resume: ResumeData,
    job: NormalizedJob,
    candidate_profile: dict,
    output_pdf_dir: str = "backend/data/tailored_resumes",
    version: int = 1
) -> TailoredResume:
    """Tailor a candidate's resume summary and skills list to match the job description."""
    # Using central Gemini retry wrapper
    
    # 1. Score candidate ATS compatibility BEFORE tailoring
    ats_score_before_obj = await calculate_ats_score(resume, job)
    ats_score_before = ats_score_before_obj.overall
    
    # Format experience and education for context
    experience_text = "\n".join([
        f"- {exp.title} at {exp.company} ({exp.start_date} - {exp.end_date or 'Present'}): {exp.description} (Tech: {', '.join(exp.technologies)})"
        for exp in resume.sections.experience
    ])

    prompt = (
        "You are an expert resume optimization and tailoring system. Rewrite the summary, skills list, "
        "and keywords list of the candidate to align with the provided Job Description (JD). Follow these strict rules:\n\n"
        
        "1. DO NOT modify the work experience list or education list (they are read-only and locked).\n"
        "2. DO NOT fabricate any experiences, skills, or technologies that the candidate does not actually possess.\n"
        "3. Rephrase the professional summary to naturally highlight skills, projects, and roles relevant to the JD.\n"
        "4. Reorder the skills list to prioritize tools and technical skills mentioned in the JD. You can only include technical skills the candidate has used in their profile or resume work history.\n"
        "5. Formulate a list of optimized ATS keywords.\n\n"
        
        f"--- JOB DESCRIPTION ---\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Description:\n{job.description}\n"
        f"Skills Required: {', '.join(job.skills)}\n\n"
        
        f"--- CANDIDATE PROFILE ---\n"
        f"Summary: {resume.sections.summary}\n"
        f"Skills: {', '.join(resume.sections.skills)}\n"
        f"Keywords: {', '.join(resume.sections.keywords)}\n"
        f"Work Experience Context:\n{experience_text}\n"
    )

    from module3.utils.gemini import generate_content_with_retry
    response = await generate_content_with_retry(
        contents=prompt,
        response_schema=LLMTailoringOutput,
        temperature=0.3
    )

    try:
        tailoring_data = json.loads(response.text)
        tailored_result = LLMTailoringOutput(**tailoring_data)
    except Exception as e:
        print("Failed to parse LLM resume tailoring output:", e)
        print("Raw response:", response.text)
        raise ValueError(f"Failed to tailor resume: {e}")

    # 2. Strict Locked Sections Integrity check (Compare to original)
    # Since we didn't pass experience/education to the LLM to rewrite (we structure the prompt to only output summary, skills, keywords),
    # we copy them verbatim from the original resume. This guarantees 100% byte/structural integrity of locked sections!
    experience_verbatim = resume.sections.experience
    education_verbatim = resume.sections.education

    # 3. Create mock tailored resume dataset to compute the ATS score AFTER tailoring
    tailored_sections = resume.sections.model_copy(update={
        "summary": tailored_result.modified_summary,
        "skills": tailored_result.modified_skills,
        "keywords": tailored_result.modified_keywords
    })
    
    tailored_resume_data = ResumeData(
        candidate_id=resume.candidate_id,
        resume_id=resume.resume_id,
        file_url=resume.file_url,
        sections=tailored_sections,
        raw_text=resume.raw_text
    )
    
    # Recalculate ATS score
    ats_score_after_obj = await calculate_ats_score(tailored_resume_data, job)
    ats_score_after = ats_score_after_obj.overall

    # 4. Compile tailored resume to PDF
    pdf_filename = f"tailored_{resume.candidate_id or 'unknown'}_{job.job_id or 'unknown'}_v{version}.pdf"
    output_pdf_path = os.path.join(output_pdf_dir, pdf_filename)
    
    # Retrieve candidate details (email, phone, etc.) from profile
    candidate_name = candidate_profile.get("name", "Candidate")
    candidate_email = candidate_profile.get("email", "email@example.com")
    candidate_phone = candidate_profile.get("phone", "")
    candidate_location = candidate_profile.get("location", "US")
    candidate_linkedin = candidate_profile.get("linkedin_url", "")
    
    # Map experience / education to simple dictionaries for ReportLab generator
    exp_list = [
        {
            "company": exp.company,
            "title": exp.title,
            "start_date": exp.start_date,
            "end_date": exp.end_date,
            "description": exp.description,
            "technologies": exp.technologies
        }
        for exp in experience_verbatim
    ]
    
    edu_list = [
        {
            "institution": edu.institution,
            "degree": edu.degree,
            "field": edu.field,
            "graduation_year": edu.graduation_year
        }
        for edu in education_verbatim
    ]
    
    print(f"Compiling tailored resume PDF to: {output_pdf_path}...")
    generate_resume_pdf(
        output_path=output_pdf_path,
        name=candidate_name,
        email=candidate_email,
        phone=candidate_phone,
        location=candidate_location,
        linkedin_url=candidate_linkedin,
        summary=tailored_result.modified_summary,
        skills=tailored_result.modified_skills,
        experience=exp_list,
        education=edu_list,
        certifications=resume.sections.certifications
    )

    return TailoredResume(
        candidate_id=resume.candidate_id or "candidate-unknown",
        job_id=job.job_id or "job-unknown",
        version=version,
        original_resume_id=resume.resume_id or "resume-unknown",
        modified_summary=tailored_result.modified_summary,
        modified_skills=tailored_result.modified_skills,
        modified_keywords=tailored_result.modified_keywords,
        experience=experience_verbatim,
        education=education_verbatim,
        pdf_url=output_pdf_path,
        ats_score_before=ats_score_before,
        ats_score_after=ats_score_after
    )
