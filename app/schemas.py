"""
Pydantic schemas for API requests/responses
"""

from datetime import datetime
from typing import List, Optional, Any, Dict
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    database: str
    total_events: int
    timestamp: str
    error: Optional[str] = None


class EventSummary(BaseModel):
    id: str
    received_at: str
    event_type: str
    action: Optional[str]
    organization: str
    repository: Optional[str]
    sender: Optional[str]
    github_id: Optional[int]
    checksum: str


class EventListResponse(BaseModel):
    events: List[Dict[str, Any]]
    count: int
    offset: int
    limit: int


class ContentVersionSummary(BaseModel):
    version_number: int
    captured_at: str
    title: Optional[str]
    body: Optional[str]
    state: Optional[str]
    metadata: Dict[str, Any]
    actor_login: str
    is_deletion: bool
    event_type: str
    action: Optional[str]
    checksum: str


class ContentHistoryResponse(BaseModel):
    content_type: str
    github_id: int
    total_versions: int
    versions: List[Dict[str, Any]]


class IntegrityCheckResponse(BaseModel):
    total_records: int
    valid_records: int
    invalid_records: int
    invalid_ids: List[str]
    verified_at: str


class WebhookResponse(BaseModel):
    status: str
    event_id: str
    event_type: str
    action: Optional[str]
    checksum: str
