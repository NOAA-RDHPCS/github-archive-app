"""
SQLAlchemy models for GitHub Archive
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Text, Integer, BigInteger, Boolean,
    DateTime, ForeignKey, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, INET, CIDR
from sqlalchemy.orm import relationship
import uuid

from database import Base


class WebhookEvent(Base):
    """Raw webhook events - complete payload storage"""
    __tablename__ = "webhook_events"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    received_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    
    # GitHub delivery metadata
    delivery_id = Column(String(64), nullable=False, unique=True)
    event_type = Column(String(64), nullable=False)
    action = Column(String(64))
    
    # Source identification
    organization = Column(String(255), nullable=False)
    repository = Column(String(255))
    sender = Column(String(255))
    
    # Complete payload
    payload = Column(JSONB, nullable=False)
    
    # Security/integrity
    signature = Column(String(128), nullable=False)
    source_ip = Column(INET, nullable=False)
    checksum = Column(String(64), nullable=False)
    
    # Indexing
    github_id = Column(BigInteger)
    
    # Metadata
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    content_versions = relationship("ContentVersion", back_populates="webhook_event")


class ContentVersion(Base):
    """Extracted content with version tracking"""
    __tablename__ = "content_versions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Link to raw event
    webhook_event_id = Column(UUID(as_uuid=True), ForeignKey("webhook_events.id"), nullable=False)
    
    # Content identification
    content_type = Column(String(64), nullable=False)  # issue, pull_request, comment, etc.
    github_id = Column(BigInteger, nullable=False)
    github_node_id = Column(String(255))
    
    # Version tracking
    version_number = Column(Integer, nullable=False, default=1)
    previous_version_id = Column(UUID(as_uuid=True), ForeignKey("content_versions.id"))
    is_deletion = Column(Boolean, nullable=False, default=False)
    
    # Content
    title = Column(Text)
    body = Column(Text)
    state = Column(String(64))
    event_metadata = Column(JSONB, nullable=False, default=dict)
    
    # Actor
    actor_login = Column(String(255), nullable=False)
    actor_id = Column(BigInteger, nullable=False)
    
    # Timestamps
    github_created_at = Column(DateTime(timezone=True))
    github_updated_at = Column(DateTime(timezone=True))
    captured_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    
    # Integrity
    checksum = Column(String(64), nullable=False)
    
    # Relationships
    webhook_event = relationship("WebhookEvent", back_populates="content_versions")
    previous_version = relationship("ContentVersion", remote_side=[id])
    
    __table_args__ = (
        UniqueConstraint('content_type', 'github_id', 'version_number', name='uq_content_version'),
    )


class AccessLog(Base):
    """Audit log for API access"""
    __tablename__ = "access_log"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    timestamp = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    
    action = Column(String(64), nullable=False)
    source_ip = Column(INET)
    user_agent = Column(Text)
    api_key_hash = Column(String(64))
    
    query_params = Column(JSONB)
    result_count = Column(Integer)
    
    success = Column(Boolean, nullable=False)
    error_message = Column(Text)


class GitHubIP(Base):
    """Cached GitHub webhook IP addresses"""
    __tablename__ = "github_ips"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ip_range = Column(CIDR, nullable=False)
    category = Column(String(64), nullable=False, default="hooks")
    fetched_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime(timezone=True), nullable=False)
    
    __table_args__ = (
        UniqueConstraint('ip_range', 'category', name='uq_github_ip'),
    )
