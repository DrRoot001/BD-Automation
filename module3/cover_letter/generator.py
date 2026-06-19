"""Cover letter generation engine using Gemini API."""
from __future__ import annotations

import os
import asyncio
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import ResumeData
from module3.tailoring.pdf_generator import generate_cover_letter_pdf

class CoverLetter(BaseModel):
    candidate_id: str
    job_id: str
    content: str
    pdf_url: str

async def generate_cover_letter(
    resume: ResumeData,
    job: NormalizedJob,
    candidate_profile: dict,
    output_pdf_dir: str = "backend/data/cover_letters"
) -> CoverLetter:
    """Generate a highly tailored cover letter PDF for a candidate and job description."""
    # Format experience for context
    experience_text = "\n".join([
        f"- {exp.title} at {exp.company} ({exp.start_date} - {exp.end_date or 'Present'}): {exp.description} (Tech: {', '.join(exp.technologies)})"
        for exp in resume.sections.experience
    ])
    
    prompt = (
        "You are an expert recruiter. Write a formal, concise, and professional cover letter (maximum 300 words, 3-4 paragraphs) "
        "for the candidate applying for the position detailed below. Follow these strict rules:\n\n"
        
        "1. DO NOT fabricate any experiences, projects, dates, or certifications. Ground the letter strictly in the candidate's work history.\n"
        "2. Address the letter to 'Hiring Manager'.\n"
        "3. Highlight technical alignments and key achievements from the candidate's resume that match the Job Description.\n"
        "4. Keep a confident, professional, and enthusiastic tone.\n"
        "5. Do NOT include placeholders (like [Date], [Company Name], [My Name]). Just write the body text of the cover letter directly.\n\n"
        
        f"--- JOB DETAILS ---\n"
        f"Role Title: {job.title}\n"
        f"Company Name: {job.company}\n"
        f"Description:\n{job.description}\n\n"
        
        f"--- CANDIDATE DETAILS ---\n"
        f"Candidate Name: {candidate_profile.get('name', 'Candidate')}\n"
        f"Work Experience History:\n{experience_text}\n"
    )

    from module3.utils.gemini import generate_content_with_retry
    response = await generate_content_with_retry(
        contents=prompt,
        temperature=0.5
    )
    
    letter_content = response.text.strip()
    
    # Render PDF cover letter
    pdf_filename = f"cover_letter_{resume.candidate_id or 'unknown'}_{job.job_id or 'unknown'}.pdf"
    output_pdf_path = os.path.join(output_pdf_dir, pdf_filename)
    
    candidate_name = candidate_profile.get("name", "Candidate")
    candidate_location = candidate_profile.get("location", "US")
    candidate_email = candidate_profile.get("email", "email@example.com")
    candidate_phone = candidate_profile.get("phone", "")
    
    candidate_info = f"{candidate_location}  |  {candidate_email}"
    if candidate_phone:
        candidate_info += f"  |  {candidate_phone}"
        
    print(f"Compiling cover letter PDF to: {output_pdf_path}...")
    generate_cover_letter_pdf(
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
