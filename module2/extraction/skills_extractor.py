"""Skills and keyword extraction from job descriptions.

Extracts technical skills and keywords from job titles and descriptions
using a predefined skill taxonomy.
"""

from __future__ import annotations

import re
from typing import List, Optional


# Predefined skill taxonomy (can be extended)
SKILL_TAXONOMY = {
    # ML / AI / Data
    "machine learning", "ml", "deep learning", "neural network", "nlp",
    "natural language processing", "computer vision", "data science",
    "ai", "artificial intelligence", "ai automation", "ml service now",
    
    # Platforms / Tools
    "servicenow", "service now", "sap", "salesforce", "jira", "confluence",
    "tableau", "power bi", "looker", "snowflake", "databricks",
    
    # Languages
    "python", "java", "javascript", "typescript", "c++", "c#", "golang", "go",
    "ruby", "php", "scala", "rust", "kotlin", "swift",
    
    # Web / Frontend
    "react", "vue", "angular", "next.js", "nextjs", "frontend", "html", "css",
    "sass", "webpack", "tailwind", "bootstrap",
    
    # Backend / Infra
    "nodejs", "node.js", "django", "flask", "fastapi", "spring", "spring boot",
    "docker", "kubernetes", "aws", "azure", "gcp", "google cloud",
    "terraform", "ansible", "jenkins", "github", "gitlab",
    
    # Databases
    "sql", "postgresql", "mysql", "mongodb", "redis", "dynamodb", "elasticsearch",
    "nosql", "database",
    
    # DevOps / SRE
    "devops", "sre", "ci/cd", "cicd", "monitoring", "observability",
    
    # Soft skills
    "communication", "leadership", "team player", "problem solving",
    "analytical", "strategic thinking",
}

# Create lowercase set for fast lookup
SKILL_TAXONOMY_LOWER = {s.lower() for s in SKILL_TAXONOMY}


def extract_skills(title: str, description: str) -> List[str]:
    """Extract technical skills from job title and description.
    
    Args:
        title: Job title.
        description: Job description.
    
    Returns:
        List of detected skills, ordered by frequency/importance.
    
    Examples:
        extract_skills("ML Engineer", "Expert in Python and TensorFlow") 
        → ["python", "machine learning", "ml"]
    """
    combined_text = (title + " " + description).lower()
    
    detected = {}  # skill -> count
    
    # Direct substring matching (order matters: longer phrases first)
    sorted_skills = sorted(SKILL_TAXONOMY_LOWER, key=len, reverse=True)
    
    for skill in sorted_skills:
        # Use word boundaries to avoid partial matches
        pattern = r"\b" + re.escape(skill) + r"\b"
        matches = len(re.findall(pattern, combined_text))
        
        if matches > 0:
            detected[skill] = detected.get(skill, 0) + matches
    
    # Sort by frequency (descending) then alphabetically
    sorted_detected = sorted(
        detected.items(),
        key=lambda x: (-x[1], x[0])
    )
    
    # Return only the skill names
    return [skill for skill, _ in sorted_detected]


def skill_match_ratio(required_skills: List[str], job_skills: List[str]) -> float:
    """Calculate the percentage of required skills found in job.
    
    Args:
        required_skills: List of required skills (from profile).
        job_skills: List of detected job skills.
    
    Returns:
        Ratio of matched skills (0.0 to 1.0).
    """
    if not required_skills:
        return 1.0
    
    job_skills_lower = {s.lower() for s in job_skills}
    required_lower = {s.lower() for s in required_skills}
    
    matched = len(required_lower & job_skills_lower)
    return matched / len(required_lower)


def add_skill(skill: str) -> None:
    """Add a new skill to the taxonomy (for customization).
    
    Args:
        skill: Skill name to add.
    """
    global SKILL_TAXONOMY_LOWER
    SKILL_TAXONOMY.add(skill)
    SKILL_TAXONOMY_LOWER.add(skill.lower())


def get_taxonomy() -> List[str]:
    """Get the current skill taxonomy.
    
    Returns:
        List of all recognized skills.
    """
    return sorted(list(SKILL_TAXONOMY))
