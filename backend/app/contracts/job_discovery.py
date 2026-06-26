"""Protocol definitions for Module 2 (Job Discovery/Parsing)."""

from typing import Protocol, List, Dict, Any

class JobDiscoveryContract(Protocol):
    async def ingest_jobs(self, raw_jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Normalize and store raw jobs into the database."""
        ...
        
    async def embed_job(self, job_id: str) -> None:
        """Generate pgvector embedding for a job."""
        ...
