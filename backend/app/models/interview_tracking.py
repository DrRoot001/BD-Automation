from sqlalchemy import Column, String, Text, ForeignKey, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
import uuid
from app.database import Base

class InterviewTracking(Base):
    __tablename__ = "interview_tracking"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id = Column(UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False)
    application_id = Column(UUID(as_uuid=True), ForeignKey("applications.id", ondelete="SET NULL"), nullable=True)
    email_subject = Column(Text)
    email_from = Column(String(255))
    received_at = Column(DateTime(timezone=True))
    interview_type = Column(String(50))
    status = Column(String(50))
    gmail_id = Column(String(255), unique=True, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
