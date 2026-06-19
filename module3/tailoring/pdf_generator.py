"""ATS-friendly PDF generator for tailored resumes and cover letters using ReportLab."""
from __future__ import annotations

import os
from pathlib import Path
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.colors import HexColor

def generate_resume_pdf(
    output_path: str,
    name: str,
    email: str,
    phone: str,
    location: str,
    linkedin_url: str,
    summary: str,
    skills: list[str],
    experience: list[dict],
    education: list[dict],
    certifications: list[str]
) -> None:
    """Generate a clean, ATS-compliant PDF resume."""
    # Ensure parent directories exist
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    # Page setup
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40
    )
    
    styles = getSampleStyleSheet()
    
    # Curated premium styling palette (Dark Slate & Neutral Gray)
    title_color = HexColor("#1E293B")
    text_color = HexColor("#334155")
    primary_color = HexColor("#0F172A")
    
    # Custom Typography Styles
    name_style = ParagraphStyle(
        'NameStyle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=24,
        textColor=primary_color,
        spaceAfter=4,
        alignment=1 # Center aligned
    )
    
    contact_style = ParagraphStyle(
        'ContactStyle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor=text_color,
        spaceAfter=15,
        alignment=1 # Center aligned
    )
    
    section_title_style = ParagraphStyle(
        'SectionTitleStyle',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=title_color,
        spaceBefore=10,
        spaceAfter=6,
        keepWithNext=True
    )
    
    body_style = ParagraphStyle(
        'BodyStyle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=13.5,
        textColor=text_color,
        spaceAfter=8
    )
    
    bold_sub_style = ParagraphStyle(
        'BoldSubStyle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=13,
        textColor=title_color,
        spaceAfter=2,
        keepWithNext=True
    )
    
    story = []
    
    # 1. Header
    story.append(Paragraph(name, name_style))
    contact_info = f"{location}  |  {email}  |  {phone}"
    if linkedin_url:
        contact_info += f"  |  {linkedin_url}"
    story.append(Paragraph(contact_info, contact_style))
    
    # 2. Professional Summary
    if summary:
        story.append(Paragraph("PROFESSIONAL SUMMARY", section_title_style))
        story.append(Paragraph(summary, body_style))
        story.append(Spacer(1, 4))
        
    # 3. Core Technical Skills
    if skills:
        story.append(Paragraph("TECHNICAL SKILLS", section_title_style))
        skills_text = ", ".join(skills)
        story.append(Paragraph(skills_text, body_style))
        story.append(Spacer(1, 4))
        
    # 4. Professional Experience
    if experience:
        story.append(Paragraph("PROFESSIONAL EXPERIENCE", section_title_style))
        for exp in experience:
            exp_story = []
            
            company = exp.get("company", "Company")
            title = exp.get("title", "Position")
            start = exp.get("start_date", "")
            end = exp.get("end_date") or "Present"
            desc = exp.get("description", "")
            techs = exp.get("technologies", [])
            
            header_text = f"{title} — {company} ({start} - {end})"
            exp_story.append(Paragraph(header_text, bold_sub_style))
            
            if techs:
                techs_text = f"<b>Technologies:</b> {', '.join(techs)}"
                exp_story.append(Paragraph(techs_text, body_style))
                
            exp_story.append(Paragraph(desc, body_style))
            exp_story.append(Spacer(1, 6))
            
            # Keep each job description together to avoid page-break splits
            story.append(KeepTogether(exp_story))
            
    # 5. Education
    if education:
        story.append(Paragraph("EDUCATION", section_title_style))
        for edu in education:
            edu_story = []
            inst = edu.get("institution", "Institution")
            degree = edu.get("degree", "")
            field = edu.get("field", "")
            year = edu.get("graduation_year")
            
            edu_text = f"<b>{degree} in {field}</b> — {inst}"
            if year:
                edu_text += f" ({year})"
            edu_story.append(Paragraph(edu_text, body_style))
            story.append(KeepTogether(edu_story))
            
    # 6. Certifications
    if certifications:
        story.append(Spacer(1, 4))
        story.append(Paragraph("CERTIFICATIONS", section_title_style))
        certs_text = ", ".join(certifications)
        story.append(Paragraph(certs_text, body_style))
        
    # Build Document
    doc.build(story)

def generate_cover_letter_pdf(
    output_path: str,
    candidate_name: str,
    candidate_info: str,
    company_name: str,
    job_title: str,
    letter_text: str
) -> None:
    """Generate a clean cover letter PDF."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=50,
        leftMargin=50,
        topMargin=50,
        bottomMargin=50
    )
    
    styles = getSampleStyleSheet()
    
    title_color = HexColor("#1E293B")
    text_color = HexColor("#334155")
    
    header_style = ParagraphStyle(
        'HeaderStyle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=14,
        leading=18,
        textColor=title_color,
        spaceAfter=15
    )
    
    body_style = ParagraphStyle(
        'BodyStyle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10.5,
        leading=15.5,
        textColor=text_color,
        spaceAfter=12
    )
    
    story = []
    
    # 1. Header (Candidate Info)
    story.append(Paragraph(candidate_name, header_style))
    story.append(Paragraph(candidate_info, body_style))
    story.append(Spacer(1, 15))
    
    # 2. Date
    from datetime import datetime
    current_date = datetime.now().strftime("%B %d, %Y")
    story.append(Paragraph(current_date, body_style))
    story.append(Spacer(1, 10))
    
    # 3. Recipient
    recipient_text = f"Hiring Manager<br/>{company_name}<br/>Re: Application for {job_title}"
    story.append(Paragraph(recipient_text, body_style))
    story.append(Spacer(1, 15))
    
    # 4. Content (Letter Body)
    # Convert newlines to breaks for HTML paragraph format
    formatted_body = letter_text.replace("\n", "<br/>")
    story.append(Paragraph(formatted_body, body_style))
    
    doc.build(story)
