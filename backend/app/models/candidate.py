from sqlalchemy import Column, String, Integer, ARRAY, Text, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
import uuid
from app.database import Base

class Candidate(Base):
    __tablename__ = "candidates"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False, unique=True)
    phone = Column(String(50))
    location = Column(String(255), default="US")
    work_auth = Column(String(50), default="us_authorized")
    tech_stack = Column(ARRAY(Text), nullable=False, default=list)
    years_exp = Column(Integer)
    linkedin_url = Column(String(500))
    gmail = Column(String(255), nullable=True)
    password = Column(String(255), nullable=True)
    google_refresh_token = Column(Text, nullable=True)
    automation_paused = Column(Integer, server_default="0", nullable=False) # 0 = false, 1 = true
    max_daily_apps_override = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    @property
    def google_connected(self) -> bool:
        return bool(self.google_refresh_token)