from sqlalchemy import Column, String, Boolean, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
import uuid

from app.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), nullable=False, unique=True)
    full_name = Column(String(255), nullable=True)
    
    from sqlalchemy.dialects.postgresql import ENUM
    role = Column(ENUM('admin', 'bd_user', name='userrole', create_type=False), nullable=False, default="bd_user")
    
    hashed_password = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # Note: supabase_user_id is NOT NULL in public.users, so we generate a random UUID string for it
    supabase_user_id = Column(String(255), nullable=False, default=lambda: str(uuid.uuid4()))
