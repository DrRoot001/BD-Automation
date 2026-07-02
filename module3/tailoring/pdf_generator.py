"""ATS-friendly PDF generator for tailored resumes and cover letters using xhtml2pdf and Jinja2."""
from __future__ import annotations

import os
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape
from xhtml2pdf import pisa

# Define the path to the templates directory
TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

def _render_pdf(html_content: str, output_path: str) -> None:
    """Helper to render HTML to PDF via xhtml2pdf."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w+b") as result_file:
        pisa_status = pisa.CreatePDF(
            src=html_content,
            dest=result_file
        )
    if pisa_status.err:
        raise RuntimeError(f"Failed to generate PDF at {output_path}")

def generate_resume_pdf(
    output_path: str,
    name: str,
    email: str,
    phone: str,
    location: str,
    linkedin_url: str,
    summary: str,
    skills: list[dict] | list[str],
    experience: list[dict],
    education: list[dict],
    certifications: list[str],
    projects: list[dict] = None,  # NEW: Added projects for rendering only
    headline: str = ""
) -> None:
    """Generate a clean, ATS-compliant PDF resume using the HTML template."""
    if projects is None:
        projects = []
        
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(['html', 'xml'])
    )
    template = env.get_template("resume_template.html")
    
    # Coerce/convert skills if it is a list of strings
    formatted_skills = []
    if skills:
        if isinstance(skills[0], str):
            formatted_skills = [{"category": "Core Skills", "keywords": skills}]
        else:
            formatted_skills = skills
            
    resume_data = {
        "basics": {
            "name": name,
            "headline": headline,
            "email": email,
            "phone": phone,
            "location": location,
            "linkedin": linkedin_url,
            "github_portfolio": ""
        },
        "summary": summary,
        "skills": formatted_skills,
        "experience": experience,
        "education": education,
        "certifications": certifications,
        "projects": projects  # Passed to template, but not returned to DB
    }
    
    html_content = template.render(resume=resume_data)
    _render_pdf(html_content, output_path)

def generate_cover_letter_pdf(
    output_path: str,
    candidate_name: str,
    candidate_info: str,
    company_name: str,
    job_title: str,
    letter_text: str
) -> None:
    """Generate a clean cover letter PDF using the HTML template."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(['html', 'xml'])
    )
    template = env.get_template("cover_letter_template.html")
    
    paragraphs = [p.strip() for p in letter_text.split('\n') if p.strip()]
    
    letter_data = {
        "hiring_manager": f"Hiring Manager at {company_name}",
        "paragraphs": paragraphs
    }
    
    resume_data = {
        "basics": {"name": candidate_name}
    }
    
    html_content = template.render(letter=letter_data, resume=resume_data)
    _render_pdf(html_content, output_path)