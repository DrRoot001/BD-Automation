"""Screening question answerer engine using Gemini API."""
from __future__ import annotations

import os
import json
import asyncio
from typing import List, Dict
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import ResumeData

class QAEntry(BaseModel):
    question: str = Field(description="The exact screening question")
    answer: str = Field(description="The drafted answer")

class QuestionAnswers(BaseModel):
    answers: List[QAEntry] = Field(description="List of screening questions and drafted answers")

async def answer_screening_questions(
    questions: List[str],
    resume: ResumeData,
    job: NormalizedJob,
    candidate_profile: dict
) -> Dict[str, str]:
    """Draft answers to application screening questions based strictly on candidate profile and resume."""
    if not questions:
        return {}
        
    # Format experience for context
    experience_text = "\n".join([
        f"- {exp.title} at {exp.company} ({exp.start_date} - {exp.end_date or 'Present'}): {exp.description} (Tech: {', '.join(exp.technologies)})"
        for exp in resume.sections.experience
    ])
    
    questions_list_text = "\n".join([f"- {q}" for q in questions])

    prompt = (
        "You are an assistant helping a candidate fill out a job application. Draft professional, concise, "
        "and factual answers for the following screening questions. Follow these rules:\n\n"
        
        "1. Be concise (1-2 sentences maximum for text answers).\n"
        "2. Ground all answers strictly in the candidate's actual profile and work history.\n"
        "3. Do NOT fabricate experience, years of experience, or technical skills.\n"
        "4. For yes/no questions about sponsorship or work authorization, use candidate_work_auth "
        "to determine if they are authorized to work (e.g., if 'us_authorized', they do not need visa sponsorship in the US).\n"
        "5. For salary expectations, use the job salary range if available (e.g. state a number aligned with the job range), "
        "or list 'Negotiable based on package'.\n\n"
        
        f"--- CANDIDATE DETAILS ---\n"
        f"Name: {candidate_profile.get('name', 'Candidate')}\n"
        f"Work Authorization: {candidate_profile.get('work_auth', 'us_authorized')}\n"
        f"Years Experience: {candidate_profile.get('years_exp', 0)}\n"
        f"Work History Context:\n{experience_text}\n\n"
        
        f"--- JOB DETAILS ---\n"
        f"Role Title: {job.title}\n"
        f"Company Name: {job.company}\n"
        f"Salary Min: {job.salary_min or 'Not specified'}\n"
        f"Salary Max: {job.salary_max or 'Not specified'}\n\n"
        
        f"--- SCREENING QUESTIONS TO ANSWER ---\n"
        f"{questions_list_text}\n"
    )

    from module3.utils.gemini import generate_content_with_retry

    response = await generate_content_with_retry(
        contents=prompt,
        response_schema=QuestionAnswers,
        temperature=0.3
    )

    try:
        data = json.loads(response.text)
        results = QuestionAnswers(**data)
        return {entry.question: entry.answer for entry in results.answers}
    except Exception as e:
        print("Failed to parse Gemini screening question answers:", e)
        print("Raw response:", response.text)
        # Fallback to simple dictionary with empty answers
        return {q: "" for q in questions}
