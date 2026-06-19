"""ATS compatibility scoring engine using Gemini API."""
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

class ATSScore(BaseModel):
    overall: float = Field(description="Overall ATS score between 0 and 100")
    keyword_match: float = Field(description="Score between 0 and 100 for JD keywords found in the resume")
    skills_overlap: float = Field(description="Score between 0 and 100 for core technical skills overlap")
    experience_relevance: float = Field(description="Score between 0 and 100 for years and domain relevance")
    education_match: float = Field(description="Score of 0 or 100 indicating if degree requirements are met")
    formatting_score: float = Field(description="Score between 0 and 100 checking the layout/formatting friendliness")
    missing_keywords: List[str] = Field(default_factory=list, description="Keywords present in the JD but absent from the resume")

class LLMATSEvaluation(BaseModel):
    keyword_match: float
    skills_overlap: float
    experience_relevance: float
    education_match: float
    formatting_score: float
    missing_keywords: List[str]

async def calculate_ats_score(resume: ResumeData, job: NormalizedJob) -> ATSScore:
    """Calculate the ATS score of a resume against a job description using Gemini."""
    # Using central Gemini retry wrapper
    
    # Structure experience and education as simple readable text for Gemini
    experience_text = "\n".join([
        f"- {exp.title} at {exp.company} ({exp.start_date} - {exp.end_date or 'Present'}): {exp.description} (Tech: {', '.join(exp.technologies)})"
        for exp in resume.sections.experience
    ])
    
    education_text = "\n".join([
        f"- {edu.degree} in {edu.field} from {edu.institution} ({edu.graduation_year or 'N/A'})"
        for edu in resume.sections.education
    ])
    
    prompt = (
        "You are an ATS (Applicant Tracking System) parser and evaluator. Score the candidate's resume "
        "compatibility against the provided Job Description (JD) across the requested dimensions:\n\n"
        
        "1. keyword_match: Heuristically score how well core keywords/phrases from the JD appear in the resume.\n"
        "2. skills_overlap: Compare technical skills required in the JD versus the candidate's skills.\n"
        "3. experience_relevance: Score relevance of years of experience and domain fields to the JD requirements.\n"
        "4. education_match: Score as 100 if candidate's degree meets JD minimum requirements, otherwise 0.\n"
        "5. formatting_score: Evaluate structure layout (sections headers clarity, standard flow, etc. typically 85-95% for standard resumes).\n"
        "6. missing_keywords: List specific technical or domain-specific keywords in the JD that are absent from the resume.\n\n"
        
        f"--- JOB DESCRIPTION ---\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location}\n"
        f"Description:\n{job.description}\n"
        f"Skills Required: {', '.join(job.skills)}\n\n"
        
        f"--- CANDIDATE RESUME ---\n"
        f"Summary: {resume.sections.summary}\n"
        f"Skills: {', '.join(resume.sections.skills)}\n"
        f"Keywords: {', '.join(resume.sections.keywords)}\n"
        f"Work Experience:\n{experience_text}\n"
        f"Education:\n{education_text}\n"
        f"Certifications: {', '.join(resume.sections.certifications)}\n"
    )

    from module3.utils.gemini import generate_content_with_retry
    response = await generate_content_with_retry(
        contents=prompt,
        response_schema=LLMATSEvaluation,
        temperature=0.1
    )

    try:
        eval_data = json.loads(response.text)
        result = LLMATSEvaluation(**eval_data)
    except Exception as e:
        print("Failed to parse LLM ATS evaluation response:", e)
        print("Raw response:", response.text)
        raise ValueError(f"Failed to calculate ATS score: {e}")

    # Compute overall ATS score using the specified weights:
    # keyword_match: 40%
    # skills_overlap: 25%
    # experience_relevance: 20%
    # education_match: 10%
    # formatting_score: 5%
    overall = (
        (result.keyword_match * 0.40) +
        (result.skills_overlap * 0.25) +
        (result.experience_relevance * 0.20) +
        (result.education_match * 0.10) +
        (result.formatting_score * 0.05)
    )
    
    # Ensure scores are within 0-100 bounds
    overall = round(max(0.0, min(100.0, overall)), 2)
    
    return ATSScore(
        overall=overall,
        keyword_match=round(result.keyword_match, 2),
        skills_overlap=round(result.skills_overlap, 2),
        experience_relevance=round(result.experience_relevance, 2),
        education_match=round(result.education_match, 2),
        formatting_score=round(result.formatting_score, 2),
        missing_keywords=result.missing_keywords
    )
