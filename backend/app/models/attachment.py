"""Attachment model."""
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, event
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..database import Base


class Attachment(Base):
    """Uploaded file metadata."""

    __tablename__ = "attachments"
    __table_args__ = (Index("ux_attachments_case_upload", "case_id", "upload_id", unique=True),)

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("experiment_cases.id"), nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    filepath = Column(String(512), nullable=False)
    file_type = Column(String(100))
    content_hash = Column(String(64), index=True)
    relative_path = Column(String(2048))
    upload_id = Column(String(36))
    index_note = Column(String(255))
    evidence_use = Column(String(160))
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())

    case = relationship("ExperimentCase", back_populates="attachments")
    knowledge_sources = relationship("KnowledgeSource", back_populates="attachment", cascade="all, delete-orphan")


class AttachmentIdWatermark(Base):
    """SQLite keeps one installation-wide watermark even after a case or file is deleted."""

    __tablename__ = "attachment_id_watermark"
    id = Column(Integer, primary_key=True)
    last_id = Column(Integer, nullable=False)


@event.listens_for(Attachment, "before_insert")
def allocate_sqlite_id(mapper, connection, item):
    if item.id is None and connection.dialect.name == "sqlite":
        from ..attachment_evidence import next_sqlite_id
        item.id = next_sqlite_id(connection)
