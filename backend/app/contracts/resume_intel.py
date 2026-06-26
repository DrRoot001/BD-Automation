"""Protocol definitions for Module 3 (Resume Intelligence/Matching)."""

from typing import Protocol, Dict, Any

class ResumeIntelligenceContract(Protocol):
    async def score_fit(self, candidate: Dict[str, Any], resume: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
        """Score how well a candidate fits a job description using LLMs."""
        ...
        
    async def tailor_resume(self, resume: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a tailored version of the resume optimized for the job."""
        ...
        
    async def generate_cover_letter(self, resume: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a cover letter optimized for the job."""
        ...
