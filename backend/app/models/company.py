from sqlalchemy import Column, String, DateTime, JSON, func
from sqlalchemy.dialects.postgresql import UUID
import uuid
from app.database import Base

class Company(Base):
    __tablename__ = "companies"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, unique=True)
    domain = Column(String(255))
    ats_type = Column(String(50))
    rate_limit_config = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())