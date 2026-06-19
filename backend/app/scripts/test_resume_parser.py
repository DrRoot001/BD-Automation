import asyncio
import os
import sys
from pathlib import Path

# Setup paths
HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "backend"))

# Load dotenv to get GEMINI_API_KEY
from dotenv import load_dotenv
load_dotenv(ROOT_DIR / "backend" / ".env")

from module3.parser.resume_parser import parse_resume

async def main():
    resume_path = ROOT_DIR / "Sabih Haider — Software Engineer _ Full-Stack Web Developer-compressed.pdf"
    if not resume_path.exists():
        print(f"✗ Resume not found at: {resume_path}")
        return
        
    print(f"Found resume at: {resume_path}")
    try:
        data = await parse_resume(str(resume_path))
        print("\n=== PARSE SUCCESSFUL ===")
        print("Summary:", data.sections.summary)
        print("\nSkills:", data.sections.skills)
        print("\nKeywords:", data.sections.keywords)
        print("\nExperience:")
        for idx, exp in enumerate(data.sections.experience):
            print(f"  [{idx+1}] Company: {exp.company} | Title: {exp.title} | Duration: {exp.start_date} - {exp.end_date or 'Present'}")
            print(f"      Tech: {exp.technologies}")
            print(f"      Desc: {exp.description[:100]}...")
            
        print("\nEducation:")
        for edu in data.sections.education:
            print(f"  - Institution: {edu.institution} | Degree: {edu.degree} | Field: {edu.field} | Year: {edu.graduation_year}")
            
        print("\nCertifications:", data.sections.certifications)
        
        # Save parsed output for inspection
        output_json = ROOT_DIR / "parsed_resume.json"
        with open(output_json, "w", encoding="utf-8") as f:
            f.write(data.sections.model_dump_json(indent=2))
        print(f"\n✓ Saved parsed resume JSON structure to: {output_json}")
    except Exception as e:
        print(f"✗ Failed parsing resume: {e}")

if __name__ == "__main__":
    asyncio.run(main())
