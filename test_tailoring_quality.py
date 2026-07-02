"""
JD Before/After Test — measures whether resume tailoring actually improves the resume.

Usage:
    python test_tailoring_quality.py [resume_pdf] [job_id]

Defaults:
    resume_pdf  → "Sabih Haider — Software Engineer _ Full-Stack Web Developer.pdf"
    job_id      → fe8b1b04-7aae-484f-9f4d-829691bbf8aa  (Backend SWE @ Palantir)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# ── Load env ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / "backend" / ".env")
sys.path.insert(0, str(ROOT))

# ── Imports ───────────────────────────────────────────────────────────────────
from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import parse_resume
from module3.scoring.ats_scorer import calculate_ats_score
from module3.scoring.fit_scorer import score_job_fit
from module3.tailoring.resume_tailor import tailor_resume

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_RESUME = ROOT / "Sabih Haider — Software Engineer _ Full-Stack Web Developer.pdf"
JOBS_FILE      = ROOT / "backend" / "app" / "jobs_data.json"
DEFAULT_JOB_ID = "fe8b1b04-7aae-484f-9f4d-829691bbf8aa"   # Backend SWE @ Palantir
OUTPUT_DIR     = str(ROOT / "backend" / "data" / "tailored_resumes")

CANDIDATE_PROFILE = {
    "id":            "test-candidate-001",
    "name":          "Sabih Haider",
    "email":         "rehanmajeed00345@gmail.com",
    "phone":         "",
    "location":      "United States",
    "work_auth":     "us_authorized",
    "years_exp":     3,
    "tech_stack":    ["Python", "React", "TypeScript", "FastAPI", "PostgreSQL"],
    "linkedin_url":  "",
}


def _sep(label: str = "", char: str = "─", width: int = 70) -> None:
    if label:
        pad = (width - len(label) - 2) // 2
        print(f"\n{'─' * pad} {label} {'─' * (width - pad - len(label) - 2)}\n")
    else:
        print(char * width)


def _score_bar(score: float, width: int = 30) -> str:
    filled = int(round(score / 100 * width))
    bar    = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {score:.1f}/100"


def _print_score_block(label: str, ats: float, fit: float | None, combined: float | None) -> None:
    print(f"  ATS Score   : {_score_bar(ats)}")
    if fit is not None:
        print(f"  Fit Score   : {_score_bar(fit)}")
    if combined is not None:
        print(f"  Combined    : {_score_bar(combined)}")


def _delta(before: float, after: float) -> str:
    diff = after - before
    sign = "+" if diff >= 0 else ""
    icon = "▲" if diff > 0 else ("▼" if diff < 0 else "=")
    return f"{sign}{diff:.1f}  {icon}"


async def run_test(resume_path: Path, job_id: str) -> None:
    # ── Load job ──────────────────────────────────────────────────────────────
    with open(JOBS_FILE, encoding="utf-8") as f:
        jobs = json.load(f)
    job_dict = next((j for j in jobs if j["id"] == job_id), None)
    if not job_dict:
        print(f"ERROR: job_id {job_id!r} not found in jobs_data.json")
        sys.exit(1)

    job = NormalizedJob(
        job_id   = job_dict["id"],
        title    = job_dict.get("title", ""),
        company  = job_dict.get("company", ""),
        location = job_dict.get("location") or "Remote",
        description = job_dict.get("description", ""),
        skills   = job_dict.get("skills") or [],
        source   = job_dict.get("source", ""),
        source_url = job_dict.get("source_url") or "",
    )

    _sep("TEST CONFIGURATION")
    print(f"  Resume : {resume_path.name}")
    print(f"  Job    : {job.title} @ {job.company}")
    print(f"  Job ID : {job.job_id}")
    print(f"  Skills : {job.skills}")
    print(f"  Desc   : {len(job.description)} chars")

    # ── Parse resume ──────────────────────────────────────────────────────────
    _sep("STEP 1 — Parsing Base Resume")
    print("  Parsing PDF with Gemini vision...")
    base_resume = await parse_resume(
        file_path    = str(resume_path),
        candidate_id = CANDIDATE_PROFILE["id"],
    )
    print(f"  Name parsed    : {getattr(base_resume.sections, 'email', '?')}")
    print(f"  Skills found   : {len(base_resume.sections.skills)}")
    print(f"  Experience     : {len(base_resume.sections.experience)} roles")
    print(f"  Education      : {len(base_resume.sections.education)} entries")

    # ── Score BEFORE ─────────────────────────────────────────────────────────
    _sep("STEP 2 — Scoring BASE Resume vs JD")
    print("  Running ATS scorer...")
    ats_before_obj = await calculate_ats_score(base_resume, job)
    ats_before     = ats_before_obj.overall

    print("  Running Fit scorer...")
    fit_before_obj = await score_job_fit(CANDIDATE_PROFILE, base_resume, job)
    fit_before     = fit_before_obj.fit_score
    combined_before = fit_before_obj.combined_score

    print()
    _print_score_block("BASE RESUME", ats_before, fit_before, combined_before)
    print(f"\n  Matching skills  : {fit_before_obj.matching_skills}")
    print(f"  Missing skills   : {fit_before_obj.missing_skills}")
    print(f"  Missing keywords : {ats_before_obj.missing_keywords}")
    print(f"\n  LLM Reasoning:\n  {fit_before_obj.reasoning}")

    # ── Tailor resume ─────────────────────────────────────────────────────────
    _sep("STEP 3 — Tailoring Resume (Fabricator Agent Loop)")
    print("  Running tailor_resume() — this may take several loops...\n")
    tailored = await tailor_resume(
        resume            = base_resume,
        job               = job,
        candidate_profile = CANDIDATE_PROFILE,
        output_pdf_dir    = OUTPUT_DIR,
        version           = 1,
    )
    print(f"\n  Tailoring complete — {tailored.pdf_url}")
    print(f"  Internal ATS tracked: {tailored.ats_score_before:.1f} → {tailored.ats_score_after:.1f}")

    # ── Build tailored ResumeData for a fresh independent scoring ─────────────
    _sep("STEP 4 — Re-parsing Tailored Resume for Independent Score")
    print("  Re-scoring tailored resume with fresh ATS + Fit calls...")

    from module3.parser.resume_parser import ResumeData, ResumeSection, ExperienceEntry, EducationEntry

    tailored_resume_data = base_resume.model_copy(deep=True)
    tailored_resume_data.sections.summary    = tailored.modified_summary
    tailored_resume_data.sections.skills     = tailored.modified_skills
    tailored_resume_data.sections.skills_categorized = tailored.modified_skills_categorized
    tailored_resume_data.sections.experience = tailored.experience
    tailored_resume_data.sections.education  = tailored.education

    ats_after_obj  = await calculate_ats_score(tailored_resume_data, job)
    ats_after      = ats_after_obj.overall

    fit_after_obj  = await score_job_fit(CANDIDATE_PROFILE, tailored_resume_data, job)
    fit_after      = fit_after_obj.fit_score
    combined_after = fit_after_obj.combined_score

    # ── Final comparison ──────────────────────────────────────────────────────
    _sep("RESULTS — Before vs After")

    rows = [
        ("ATS Score",      ats_before,      ats_after),
        ("Fit Score",      fit_before,      fit_after),
        ("Combined Score", combined_before, combined_after),
    ]

    col_w = 20
    print(f"  {'Metric':<{col_w}} {'Before':>10}   {'After':>10}   {'Delta':>12}")
    print(f"  {'─'*col_w} {'─'*10}   {'─'*10}   {'─'*12}")
    for name, before, after in rows:
        print(f"  {name:<{col_w}} {before:>10.1f}   {after:>10.1f}   {_delta(before, after):>12}")

    print()
    print(f"  TAILORED Missing skills   : {fit_after_obj.missing_skills}")
    print(f"  TAILORED Missing keywords : {ats_after_obj.missing_keywords}")
    print(f"\n  TAILORED LLM Reasoning:\n  {fit_after_obj.reasoning}")

    _sep("VERDICT")
    ats_improved  = ats_after      > ats_before
    fit_improved  = fit_after      > fit_before
    comb_improved = combined_after > combined_before

    if ats_improved and comb_improved:
        verdict = "PASS — tailoring improved both ATS and combined score"
    elif comb_improved:
        verdict = "PARTIAL — combined score improved (ATS flat/down)"
    elif ats_improved:
        verdict = "PARTIAL — ATS improved but combined score did not"
    else:
        verdict = "FAIL — tailoring did not improve any score"

    print(f"  {verdict}")
    print(f"  PDF saved: {tailored.pdf_url}")
    _sep()


if __name__ == "__main__":
    resume_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_RESUME
    job_id      = sys.argv[2]      if len(sys.argv) > 2 else DEFAULT_JOB_ID

    if not resume_path.exists():
        print(f"ERROR: resume not found: {resume_path}")
        sys.exit(1)

    asyncio.run(run_test(resume_path, job_id))
