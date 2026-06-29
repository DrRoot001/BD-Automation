from sqlalchemy import Column, String, ForeignKey, DateTime, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
import uuid
from app.database import Base

class ApplicationHistory(Base):
    __tablename__ = "application_history"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id = Column(UUID(as_uuid=True), ForeignKey("applications.id"), nullable=False)
    from_status = Column(String(50), nullable=True)
    to_status = Column(String(50), nullable=False)
    meta_data = Column(JSONB)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __init__(self, **kwargs):
        if 'metadata' in kwargs:
            kwargs['meta_data'] = kwargs.pop('metadata')
        super().__init__(**kwargs)