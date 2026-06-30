from sqlalchemy import Column, String, Integer, ARRAY, Text, DateTime, Boolean, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from pgvector.sqlalchemy import Vector  # <-- import from pgvector
import uuid
from app.database import Base

class Job(Base):
    __tablename__ = "jobs"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(500), nullable=False)
    company = Column(String(255), nullable=False)
    location = Column(String(255))
    source = Column(String(100), nullable=False)
    source_url = Column(String(2000), nullable=False, unique=True)
    canonical_url = Column(String(2000))
    description = Column(Text)
    skills = Column(ARRAY(Text), default=list)
    salary_min = Column(Integer)
    salary_max = Column(Integer)
    pay_period = Column(String(20))
    job_type = Column(String(20))
    posted_at = Column(DateTime(timezone=True))
    embedding = Column(Vector(1536))  # <-- use Vector from pgvector
    is_duplicate = Column(Boolean, default=False)
    duplicate_of = Column(UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    ats_type = Column(String(100))
    job_category = Column(String(100))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)