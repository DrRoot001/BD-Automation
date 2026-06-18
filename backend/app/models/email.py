from sqlalchemy import Column, String, Text, DateTime, Numeric, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
import uuid
from app.database import Base

class Email(Base):
    __tablename__ = "emails"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id = Column(UUID(as_uuid=True), ForeignKey("candidates.id"), nullable=False)
    application_id = Column(UUID(as_uuid=True), ForeignKey("applications.id"), nullable=True)
    gmail_id = Column(String(255), nullable=False, unique=True)
    from_addr = Column(String(255))
    subject = Column(Text)
    body_text = Column(Text)
    classification = Column(String(50))
    confidence = Column(Numeric(3, 2))
    raw_json = Column(JSONB)
    received_at = Column(DateTime(timezone=True))
    processed_at = Column(DateTime(timezone=True))