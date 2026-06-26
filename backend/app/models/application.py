from sqlalchemy import Column, String, Integer, Numeric, Text, ForeignKey, DateTime, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
import uuid
from app.database import Base

class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("candidate_id", "job_id", name="uq_applications_candidate_job"),)
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id = Column(UUID(as_uuid=True), ForeignKey("candidates.id"), nullable=False)
    job_id = Column(UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=False)
    resume_id = Column(UUID(as_uuid=True), ForeignKey("resumes.id"), nullable=True)
    cover_letter_url = Column(String(1000))
    status = Column(String(50), nullable=False, default="FOUND")
    fit_score = Column(Numeric(5, 2))
    ats_score = Column(Numeric(5, 2))
    combined_score = Column(Numeric(5, 2))
    screenshot_url = Column(String(1000))
    submitted_at = Column(DateTime(timezone=True))
    error_message = Column(Text)
    failure_reason = Column(String(50), nullable=True)
    retry_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())