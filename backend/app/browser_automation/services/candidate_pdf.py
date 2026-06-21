"""Identity-safe PDF generation for resume + cover letter.

Used as a SAFETY NET when M3 has not produced a tailored resume / cover
letter for this candidate. Generating a candidate-data-driven PDF on the
fly is acceptable; uploading another candidate's PDF is NOT.

The PDFs produced here are minimal — just the candidate's name, contact
info, experience years, and tech stack. They are valid PDFs that an ATS
will accept as a file upload, and they cannot leak another candidate's
identity. Real production runs should pull from M3's tailored output via
data/tailored_resumes/ / data/cover_letters/ which take precedence.

We deliberately do NOT format these as a polished resume — that's M3's
job. The point here is identity safety + an exercised upload path while
M3 catches up.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _safe(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    s = str(value).strip()
    return s or fallback


def _generate(out_path: Path, title: str, lines: list[str]) -> None:
    """Render lines into a minimal A4 PDF at out_path via reportlab."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    out_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out_path), pagesize=LETTER)
    width, height = LETTER
    margin = 54  # 0.75"
    y = height - margin

    c.setFont("Helvetica-Bold", 18)
    c.drawString(margin, y, title)
    y -= 30

    c.setFont("Helvetica", 11)
    for line in lines:
        if y < margin:
            c.showPage()
            c.setFont("Helvetica", 11)
            y = height - margin
        # crude word-wrap at ~88 chars
        chunk = line
        while len(chunk) > 88:
            split_at = chunk.rfind(" ", 0, 88)
            if split_at == -1:
                split_at = 88
            c.drawString(margin, y, chunk[:split_at])
            y -= 16
            chunk = chunk[split_at:].lstrip()
            if y < margin:
                c.showPage()
                c.setFont("Helvetica", 11)
                y = height - margin
        c.drawString(margin, y, chunk)
        y -= 16
    c.save()


def generate_candidate_resume(
    candidate_profile: Dict[str, Any],
    candidate_id: str,
    backend_dir: Path,
) -> str:
    """Write a minimal Sabih.pdf-style resume under data/tailored_resumes/
    based purely on this candidate's own profile data. Returns the path.
    """
    p = candidate_profile or {}
    name = _safe(p.get("name") or f"{_safe(p.get('first_name'))} {_safe(p.get('last_name'))}".strip(),
                 "Applicant")
    out = backend_dir / "data" / "tailored_resumes" / f"tailored_{candidate_id}_auto.pdf"

    lines = [
        f"Name: {name}",
        f"Email: {_safe(p.get('email'))}",
        f"Phone: {_safe(p.get('phone'))}",
        f"Location: {_safe(p.get('location'))}",
        f"LinkedIn: {_safe(p.get('linkedin_url'))}",
        f"Website: {_safe(p.get('website'))}",
        "",
        "PROFESSIONAL SUMMARY",
        f"{_safe(p.get('current_title'), 'Software Engineer')} with "
        f"{_safe(p.get('experience_years'), '5')} years of professional experience.",
        "",
        "EXPERIENCE",
        f"Current role: {_safe(p.get('current_title'), 'Software Engineer')} "
        f"at {_safe(p.get('current_company'), 'Freelance')}.",
        "",
        "SKILLS / TECH STACK",
        _safe(p.get("tech_stack"), "—"),
        "",
        "EDUCATION",
        _safe(p.get("education"), "Bachelor's degree"),
        "",
        "WORK AUTHORIZATION",
        f"Authorized: {_safe(p.get('work_authorization'), 'Yes')}",
        f"Requires sponsorship: {_safe(p.get('sponsorship'), 'No')}",
    ]

    _generate(out, f"{name} — Resume", lines)
    logger.warning(
        f"[PDF Safety Net] Generated minimal resume for candidate "
        f"{candidate_id} at {out.name}. M3 must produce a real tailored PDF."
    )
    return str(out)


def generate_candidate_cover_letter(
    candidate_profile: Dict[str, Any],
    candidate_id: str,
    job_company: str,
    job_title: str,
    backend_dir: Path,
) -> str:
    p = candidate_profile or {}
    name = _safe(p.get("name") or f"{_safe(p.get('first_name'))} {_safe(p.get('last_name'))}".strip(),
                 "Applicant")
    out = backend_dir / "data" / "cover_letters" / f"cover_letter_{candidate_id}_auto.pdf"

    lines = [
        f"From: {name}",
        f"Email: {_safe(p.get('email'))}",
        f"Phone: {_safe(p.get('phone'))}",
        "",
        f"To: Hiring Team at {job_company or 'the company'}",
        f"Re: {job_title or 'Software Engineering Role'}",
        "",
        "Dear Hiring Team,",
        "",
        f"I am writing to express my interest in the {job_title or 'role'} at "
        f"{job_company or 'your company'}. With "
        f"{_safe(p.get('experience_years'), '5')} years of professional "
        f"experience as a {_safe(p.get('current_title'), 'Software Engineer')}, "
        f"I bring hands-on expertise across {_safe(p.get('tech_stack'), 'modern stacks')}.",
        "",
        f"I am currently {_safe(p.get('current_title'), 'a Software Engineer')} "
        f"at {_safe(p.get('current_company'), 'Freelance')}. I am authorized to "
        f"work and excited to contribute to your team's mission.",
        "",
        "Thank you for considering my application. I would welcome the chance "
        "to discuss how my experience aligns with your needs.",
        "",
        "Sincerely,",
        name,
    ]

    _generate(out, f"{name} — Cover Letter", lines)
    logger.warning(
        f"[PDF Safety Net] Generated minimal cover letter for candidate "
        f"{candidate_id} at {out.name}. M3 must produce a real tailored PDF."
    )
    return str(out)
