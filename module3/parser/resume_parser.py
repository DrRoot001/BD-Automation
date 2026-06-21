"""Resume parser engine using pdfplumber and Gemini API for structured JSON extraction."""
from __future__ import annotations

import os
import asyncio
import json
import pdfplumber
import pypdfium2 as pdfium
from PIL import Image
from typing import List, Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

class ExperienceEntry(BaseModel):
    company: str = Field(description="Name of the company or organization")
    title: str = Field(description="Job title")
    start_date: str = Field(description="Start date of employment (e.g., 'Jan 2020' or '2020')")
    end_date: Optional[str] = Field(None, description="End date of employment or None/Present if current")
    description: str = Field(description="Work description or responsibilities")
    technologies: List[str] = Field(default_factory=list, description="Technologies, programming languages, or tools used in this job")

class EducationEntry(BaseModel):
    institution: str = Field(description="Name of the school, university, or college")
    degree: str = Field(description="Degree name (e.g., 'Bachelor of Science' or 'BS')")
    field: str = Field(description="Field of study (e.g., 'Computer Science')")
    graduation_year: Optional[int] = Field(None, description="Graduation year (4-digit integer)")

class ResumeSection(BaseModel):
    summary: str = Field(description="Professional summary or profile description")
    current_company: Optional[str] = Field(None, description="Name of the candidate's current or most recent employer company. Return None if not explicitly clear.")
    current_title: Optional[str] = Field(None, description="Candidate's current or most recent job title. Return None if not explicitly clear.")
    salary_expectation: Optional[str] = Field(None, description="Any mention of salary expectations or current salary. Return None if absent.")
    website: Optional[str] = Field(None, description="Personal website, portfolio, GitHub, or LinkedIn URL extracted from contact info. Return None if absent.")
    skills: List[str] = Field(default_factory=list, description="Technical and soft skills")
    keywords: List[str] = Field(default_factory=list, description="Core keywords extracted from summary + skills + experience")
    experience: List[ExperienceEntry] = Field(default_factory=list, description="Employment and work experience details")
    education: List[EducationEntry] = Field(default_factory=list, description="Education details")
    certifications: List[str] = Field(default_factory=list, description="List of professional certifications")

class ResumeData(BaseModel):
    candidate_id: Optional[str] = None
    resume_id: Optional[str] = None
    file_url: str
    file_hash: Optional[str] = None
    sections: ResumeSection
    raw_text: str

def extract_pdf_text(file_path: str) -> str:
    """Extract raw text from a PDF file."""
    text_content = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_content.append(page_text)
    return "\n".join(text_content)

def render_pdf_to_images(file_path: str) -> List[Image.Image]:
    """Render PDF pages to PIL images."""
    doc = pdfium.PdfDocument(file_path)
    images = []
    for page in doc:
        bitmap = page.render(scale=2)
        images.append(bitmap.to_pil())
    return images

async def parse_resume(file_path: str, candidate_id: Optional[str] = None, resume_id: Optional[str] = None) -> ResumeData:
    """Parse a PDF resume into structured ResumeData using Gemini API. Handles both text and image-based PDFs."""
    temp_file_path = None
    original_url = file_path
    if file_path.startswith("http://") or file_path.startswith("https://"):
        print(f"Downloading remote resume: {file_path}")
        import httpx
        import tempfile
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(file_path)
                resp.raise_for_status()
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                    tmp.write(resp.content)
                    temp_file_path = tmp.name
            file_path = temp_file_path
        except Exception as e:
            raise FileNotFoundError(f"Failed to download remote resume from {file_path}: {e}")
            
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Resume file not found at: {file_path}")
        
    print(f"Extracting text from PDF resume: {file_path}...")
    raw_text = ""
    try:
        raw_text = extract_pdf_text(file_path)
    except Exception as e:
        print(f"pdfplumber failed to extract text: {e}. Falling back to image rendering.")
        raw_text = ""

    from module3.utils.gemini import generate_content_with_retry

    prompt = (
        "You are an expert resume parsing system. Analyze the following candidate's resume "
        "and structure it into the requested JSON schema. Make sure to ground all work experience, dates, "
        "institution names, and degrees strictly in the provided content. Do not make up any certifications, "
        "work experience, or education details."
    )

    # Decide if we need multimodal parsing
    if len(raw_text.strip()) < 100:
        print(f"Extracted text length is low ({len(raw_text)} chars). Rendering PDF pages as images for multimodal parsing...")
        try:
            images = render_pdf_to_images(file_path)
            contents = [prompt] + images
            
            response = await generate_content_with_retry(
                contents=contents,
                response_schema=ResumeSection,
                temperature=0.1
            )
            
            # Since text was missing/empty, let's set raw_text to a placeholder or the parsed summary
            raw_text = f"[Image-Based PDF parsed multimodally]"
        except Exception as img_err:
            print("Failed to run image rendering or generate content:", img_err)
            raise ValueError(f"Multimodal parsing failed: {img_err}")
    else:
        print("Using extracted text for parsing...")
        full_prompt = f"{prompt}\n\n--- RAW RESUME TEXT ---\n{raw_text}\n--- END RAW TEXT ---"
        response = await generate_content_with_retry(
            contents=full_prompt,
            response_schema=ResumeSection,
            temperature=0.1
        )
    
    # Parse the JSON response
    try:
        data = json.loads(response.text)
        sections = ResumeSection(**data)
    except Exception as e:
        print("Failed to parse Gemini output as ResumeSection:", e)
        print("Raw response:", response.text)
        raise ValueError(f"Failed to structure resume data: {e}")
        
    # Calculate file hash for idempotency if a valid file path was provided
    file_hash = None
    if os.path.exists(file_path):
        import hashlib
        with open(file_path, "rb") as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()
            
    if temp_file_path and os.path.exists(temp_file_path):
        try:
            os.remove(temp_file_path)
        except OSError:
            pass
            
    return ResumeData(
        candidate_id=candidate_id,
        resume_id=resume_id,
        file_url=original_url,
        file_hash=file_hash,
        sections=sections,
        raw_text=raw_text
    )
