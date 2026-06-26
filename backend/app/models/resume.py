from sqlalchemy import Column, String, Integer, Boolean, ForeignKey, DateTime, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from pgvector.sqlalchemy import Vector
import uuid
from app.database import Base

class Resume(Base):
    __tablename__ = "resumes"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id = Column(UUID(as_uuid=True), ForeignKey("candidates.id"), nullable=False)
    version = Column(Integer, nullable=False)
    file_url = Column(String(1000), nullable=False)
    parsed_json = Column(JSONB)
    is_base = Column(Boolean, default=False)
    tailored_for_job_id = Column(UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    embedding = Column(Vector(1536))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    __table_args__ = (UniqueConstraint('candidate_id', 'version'),)