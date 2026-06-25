from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional, Dict, Any
from uuid import UUID

class ResumeCreate(BaseModel):
    candidate_id: UUID
    version: int
    file_url: str
    parsed_json: Optional[Dict[str, Any]] = None
    is_base: Optional[bool] = False
    tailored_for_job_id: Optional[UUID] = None

class ResumeResponse(ResumeCreate):
    model_config = ConfigDict(from_attributes=True)
    
    id: UUID
    created_at: datetime