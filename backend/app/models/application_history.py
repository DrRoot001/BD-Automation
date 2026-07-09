from sqlalchemy import Column, String, ForeignKey, DateTime, func, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
import uuid
from app.database import Base

class ApplicationHistory(Base):
    __tablename__ = "application_history"

    # Composite index for the dashboard's per-row LATERAL lookup
    # (WHERE application_id = :id ORDER BY created_at DESC LIMIT 1). Postgres scans
    # this btree backward for the DESC ordering, so no separate sort is needed.
    # An FK alone does NOT create an index in Postgres, so without this the LATERAL
    # seq-scanned application_history once per application row on the dashboard.
    __table_args__ = (
        Index("ix_application_history_app_created", "application_id", "created_at"),
    )

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