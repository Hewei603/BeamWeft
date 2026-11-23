"""Immutable, owner-scoped optical component model revisions."""
from sqlalchemy import Column, Integer, JSON, String, UniqueConstraint

from ..database import Base


class ComponentModelRevision(Base):
    __tablename__ = "component_model_revisions"
    __table_args__ = (
        UniqueConstraint("owner_key", "family", "version", name="uq_component_model_version"),
        UniqueConstraint("owner_key", "receipt", name="uq_component_model_capture"),
    )

    id = Column(Integer, primary_key=True)
    owner_key = Column(String(80), nullable=False, index=True)
    family = Column(String(36), nullable=False)
    version = Column(Integer, nullable=False)
    receipt = Column(String(64), nullable=False)
    snapshot = Column(JSON, nullable=False)
