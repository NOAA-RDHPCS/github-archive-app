"""
GitHub Archive Service - Main Application
FastAPI-based webhook receiver with immutable storage
"""

import hashlib
import hmac
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Request, HTTPException, Depends, Query, Header
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, engine, async_session
from models import Base, WebhookEvent, ContentVersion, AccessLog, GitHubIP
from schemas import (
    HealthResponse, 
    EventListResponse, 
    ContentHistoryResponse,
    IntegrityCheckResponse
)
from security import verify_signature, check_ip_allowed, get_github_ips
from events import process_event

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration from environment
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
ALLOWED_ORGS = [org.strip() for org in os.environ.get("ALLOWED_ORGS", "").split(",") if org.strip()]
IP_ALLOWLIST_MODE = os.environ.get("IP_ALLOWLIST_MODE", "github")
CUSTOM_ALLOWED_IPS = os.environ.get("CUSTOM_ALLOWED_IPS", "").split(",")
READ_API_ENABLED = os.environ.get("READ_API_ENABLED", "true").lower() == "true"
READ_API_AUTH_REQUIRED = os.environ.get("READ_API_AUTH_REQUIRED", "false").lower() == "true"
READ_API_KEY = os.environ.get("READ_API_KEY", "")
READ_API_MAX_RESULTS = int(os.environ.get("READ_API_MAX_RESULTS", "1000"))
ARCHIVE_PATH = os.environ.get("ARCHIVE_PATH", "/var/lib/github-archive/json")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown"""
    # Startup
    logger.info("Starting GitHub Archive Service")
    
    # Create database tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Initial GitHub IP fetch
    if IP_ALLOWLIST_MODE == "github":
        await update_github_ips()
    
    logger.info(f"Configured for organizations: {ALLOWED_ORGS}")
    logger.info(f"IP allowlist mode: {IP_ALLOWLIST_MODE}")
    
    yield
    
    # Shutdown
    logger.info("Shutting down GitHub Archive Service")
    await engine.dispose()


app = FastAPI(
    title="GitHub Archive Service",
    description="Immutable archive for GitHub organization activity",
    version="1.0.0",
    lifespan=lifespan
)


async def update_github_ips():
    """Fetch and cache GitHub webhook IP addresses"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("https://api.github.com/meta", timeout=30.0)
            response.raise_for_status()
            data = response.json()
            
            hooks_ips = data.get("hooks", [])
            
            async with async_session() as session:
                # Clear old IPs and insert new ones
                await session.execute(text("DELETE FROM github_ips WHERE category = 'hooks'"))
                
                for ip_range in hooks_ips:
                    github_ip = GitHubIP(
                        ip_range=ip_range,
                        category="hooks",
                        expires_at=datetime.now(timezone.utc).replace(hour=23, minute=59, second=59)
                    )
                    session.add(github_ip)
                
                await session.commit()
                logger.info(f"Updated GitHub IPs: {len(hooks_ips)} ranges cached")
                
    except Exception as e:
        logger.error(f"Failed to update GitHub IPs: {e}")


def compute_checksum(data: dict) -> str:
    """Compute SHA-256 checksum of data"""
    json_str = json.dumps(data, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(json_str.encode()).hexdigest()


def compute_content_checksum(title: str, body: str, metadata: dict) -> str:
    """Compute checksum for content version"""
    content = {
        "title": title or "",
        "body": body or "",
        "metadata": metadata or {}
    }
    return compute_checksum(content)


async def write_json_archive(event_id: str, event_type: str, payload: dict):
    """Write JSON backup file to archive directory"""
    try:
        now = datetime.now(timezone.utc)
        dir_path = os.path.join(
            ARCHIVE_PATH,
            now.strftime("%Y"),
            now.strftime("%m"),
            now.strftime("%d"),
            now.strftime("%H")
        )
        os.makedirs(dir_path, exist_ok=True)
        
        file_path = os.path.join(dir_path, f"{event_id}.json")
        
        archive_data = {
            "event_id": event_id,
            "event_type": event_type,
            "archived_at": now.isoformat(),
            "payload": payload
        }
        
        with open(file_path, 'w') as f:
            json.dump(archive_data, f, indent=2, default=str)
        
        # Make file read-only
        os.chmod(file_path, 0o444)
        
        logger.debug(f"Archived event {event_id} to {file_path}")
        
    except Exception as e:
        logger.error(f"Failed to write JSON archive for {event_id}: {e}")


async def log_access(
    session: AsyncSession,
    action: str,
    request: Request,
    query_params: dict = None,
    result_count: int = None,
    success: bool = True,
    error_message: str = None
):
    """Log API access to audit table"""
    try:
        log_entry = AccessLog(
            action=action,
            source_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            api_key_hash=hashlib.sha256(
                request.headers.get("x-api-key", "").encode()
            ).hexdigest()[:16] if request.headers.get("x-api-key") else None,
            query_params=query_params,
            result_count=result_count,
            success=success,
            error_message=error_message
        )
        session.add(log_entry)
        await session.commit()
    except Exception as e:
        logger.error(f"Failed to log access: {e}")


# =============================================================================
# HEALTH & STATUS ENDPOINTS
# =============================================================================

@app.get("/health", response_model=HealthResponse)
async def health_check(db: AsyncSession = Depends(get_db)):
    """Health check endpoint"""
    try:
        # Check database connectivity
        result = await db.execute(text("SELECT 1"))
        db_healthy = result.scalar() == 1
        
        # Get event counts
        event_count = await db.execute(text("SELECT COUNT(*) FROM webhook_events"))
        total_events = event_count.scalar()
        
        return HealthResponse(
            status="healthy" if db_healthy else "unhealthy",
            database="connected" if db_healthy else "disconnected",
            total_events=total_events,
            timestamp=datetime.now(timezone.utc).isoformat()
        )
    except Exception as e:
        return HealthResponse(
            status="unhealthy",
            database="error",
            total_events=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
            error=str(e)
        )


@app.get("/metrics")
async def metrics(db: AsyncSession = Depends(get_db)):
    """Prometheus-compatible metrics endpoint"""
    try:
        # Get counts by event type
        result = await db.execute(text("""
            SELECT event_type, COUNT(*) as count 
            FROM webhook_events 
            GROUP BY event_type
        """))
        event_counts = result.fetchall()
        
        # Get total counts
        total = await db.execute(text("SELECT COUNT(*) FROM webhook_events"))
        total_events = total.scalar()
        
        content_total = await db.execute(text("SELECT COUNT(*) FROM content_versions"))
        total_content = content_total.scalar()
        
        # Format as Prometheus metrics
        lines = [
            "# HELP github_archive_events_total Total webhook events received",
            "# TYPE github_archive_events_total counter",
            f"github_archive_events_total {total_events}",
            "",
            "# HELP github_archive_content_versions_total Total content versions stored",
            "# TYPE github_archive_content_versions_total counter",
            f"github_archive_content_versions_total {total_content}",
            "",
            "# HELP github_archive_events_by_type Events by type",
            "# TYPE github_archive_events_by_type counter",
        ]
        
        for event_type, count in event_counts:
            lines.append(f'github_archive_events_by_type{{type="{event_type}"}} {count}')
        
        return "\n".join(lines)
        
    except Exception as e:
        return f"# Error generating metrics: {e}"


# =============================================================================
# WEBHOOK ENDPOINT
# =============================================================================

@app.post("/webhook")
async def receive_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_github_event: str = Header(...),
    x_github_delivery: str = Header(...),
    x_hub_signature_256: str = Header(...)
):
    """
    Main webhook receiver endpoint.
    Validates signature, checks IP, and stores event immutably.
    """
    client_ip = request.client.host if request.client else "unknown"
    
    # Check IP allowlist
    if IP_ALLOWLIST_MODE != "disabled":
        allowed = await check_ip_allowed(db, client_ip, IP_ALLOWLIST_MODE, CUSTOM_ALLOWED_IPS)
        if not allowed:
            logger.warning(f"Rejected webhook from non-allowed IP: {client_ip}")
            raise HTTPException(status_code=403, detail="IP not allowed")
    
    # Get raw body for signature verification
    body = await request.body()
    
    # Verify webhook signature
    if not verify_signature(body, x_hub_signature_256, WEBHOOK_SECRET):
        logger.warning(f"Invalid webhook signature from {client_ip}")
        raise HTTPException(status_code=401, detail="Invalid signature")
    
    # Parse payload
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    
    # Extract organization
    org_name = None
    if "organization" in payload:
        org_name = payload["organization"].get("login")
    elif "repository" in payload:
        org_name = payload["repository"].get("owner", {}).get("login")
    
    # Verify organization is allowed
    if ALLOWED_ORGS and org_name not in ALLOWED_ORGS:
        logger.warning(f"Rejected webhook from non-allowed org: {org_name}")
        raise HTTPException(status_code=403, detail="Organization not allowed")
    
    # Extract common fields
    action = payload.get("action")
    repo_name = payload.get("repository", {}).get("full_name") if payload.get("repository") else None
    sender = payload.get("sender", {}).get("login")
    
    # Compute checksum
    checksum = compute_checksum(payload)
    
    # Extract GitHub ID if present
    github_id = None
    for key in ["issue", "pull_request", "comment", "discussion", "review", "release", "milestone"]:
        if key in payload and payload[key]:
            github_id = payload[key].get("id")
            break
    
    # Create event record
    event_id = str(uuid.uuid4())
    event = WebhookEvent(
        id=event_id,
        delivery_id=x_github_delivery,
        event_type=x_github_event,
        action=action,
        organization=org_name or "unknown",
        repository=repo_name,
        sender=sender,
        payload=payload,
        signature=x_hub_signature_256,
        source_ip=client_ip,
        checksum=checksum,
        github_id=github_id
    )
    
    try:
        db.add(event)
        await db.flush()
        
        # Process event to extract content versions
        await process_event(db, event, payload, x_github_event, action)
        
        await db.commit()
        
        # Write JSON backup
        await write_json_archive(event_id, x_github_event, payload)
        
        logger.info(f"Archived {x_github_event}.{action} from {org_name}/{repo_name} (delivery: {x_github_delivery})")
        
        return JSONResponse(
            status_code=201,
            content={
                "status": "archived",
                "event_id": event_id,
                "event_type": x_github_event,
                "action": action,
                "checksum": checksum
            }
        )
        
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to archive event: {e}")
        raise HTTPException(status_code=500, detail="Failed to archive event")


# =============================================================================
# READ API ENDPOINTS
# =============================================================================

async def verify_api_key(request: Request):
    """Verify API key if authentication is required"""
    if not READ_API_AUTH_REQUIRED:
        return True
    
    api_key = request.headers.get("x-api-key")
    if not api_key or api_key != READ_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return True


@app.get("/api/v1/events", response_model=EventListResponse)
async def list_events(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_api_key),
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    organization: Optional[str] = Query(None, description="Filter by organization"),
    repository: Optional[str] = Query(None, description="Filter by repository"),
    since: Optional[str] = Query(None, description="Events after this ISO timestamp"),
    until: Optional[str] = Query(None, description="Events before this ISO timestamp"),
    limit: int = Query(100, ge=1, le=READ_API_MAX_RESULTS),
    offset: int = Query(0, ge=0)
):
    """List archived webhook events with optional filtering"""
    if not READ_API_ENABLED:
        raise HTTPException(status_code=404, detail="Read API not enabled")
    
    # Build query
    query = "SELECT id, received_at, event_type, action, organization, repository, sender, github_id, checksum FROM webhook_events WHERE 1=1"
    params = {}
    
    if event_type:
        query += " AND event_type = :event_type"
        params["event_type"] = event_type
    
    if organization:
        query += " AND organization = :organization"
        params["organization"] = organization
    
    if repository:
        query += " AND repository = :repository"
        params["repository"] = repository
    
    if since:
        query += " AND received_at >= :since"
        params["since"] = since
    
    if until:
        query += " AND received_at <= :until"
        params["until"] = until
    
    query += " ORDER BY received_at DESC LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset
    
    result = await db.execute(text(query), params)
    events = result.fetchall()
    
    # Log access
    await log_access(
        db, "list_events", request,
        query_params={"event_type": event_type, "organization": organization, "limit": limit},
        result_count=len(events)
    )
    
    return EventListResponse(
        events=[
            {
                "id": str(e.id),
                "received_at": e.received_at.isoformat(),
                "event_type": e.event_type,
                "action": e.action,
                "organization": e.organization,
                "repository": e.repository,
                "sender": e.sender,
                "github_id": e.github_id,
                "checksum": e.checksum
            }
            for e in events
        ],
        count=len(events),
        offset=offset,
        limit=limit
    )


@app.get("/api/v1/events/{event_id}")
async def get_event(
    event_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_api_key)
):
    """Get a specific event by ID, including full payload"""
    if not READ_API_ENABLED:
        raise HTTPException(status_code=404, detail="Read API not enabled")
    
    result = await db.execute(
        text("SELECT * FROM webhook_events WHERE id = :id"),
        {"id": event_id}
    )
    event = result.fetchone()
    
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    
    await log_access(db, "get_event", request, query_params={"event_id": event_id}, result_count=1)
    
    return {
        "id": str(event.id),
        "received_at": event.received_at.isoformat(),
        "delivery_id": event.delivery_id,
        "event_type": event.event_type,
        "action": event.action,
        "organization": event.organization,
        "repository": event.repository,
        "sender": event.sender,
        "payload": event.payload,
        "checksum": event.checksum,
        "source_ip": str(event.source_ip)
    }


@app.get("/api/v1/content/{content_type}/{github_id}/history", response_model=ContentHistoryResponse)
async def get_content_history(
    content_type: str,
    github_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_api_key)
):
    """Get full version history for a content item (issue, PR, comment, etc.)"""
    if not READ_API_ENABLED:
        raise HTTPException(status_code=404, detail="Read API not enabled")
    
    result = await db.execute(
        text("""
            SELECT cv.*, we.organization, we.repository, we.event_type, we.action
            FROM content_versions cv
            JOIN webhook_events we ON cv.webhook_event_id = we.id
            WHERE cv.content_type = :content_type AND cv.github_id = :github_id
            ORDER BY cv.version_number ASC
        """),
        {"content_type": content_type, "github_id": github_id}
    )
    versions = result.fetchall()
    
    if not versions:
        raise HTTPException(status_code=404, detail="Content not found")
    
    await log_access(
        db, "get_content_history", request,
        query_params={"content_type": content_type, "github_id": github_id},
        result_count=len(versions)
    )
    
    return ContentHistoryResponse(
        content_type=content_type,
        github_id=github_id,
        total_versions=len(versions),
        versions=[
            {
                "version_number": v.version_number,
                "captured_at": v.captured_at.isoformat(),
                "title": v.title,
                "body": v.body,
                "state": v.state,
                "metadata": v.metadata,
                "actor_login": v.actor_login,
                "is_deletion": v.is_deletion,
                "event_type": v.event_type,
                "action": v.action,
                "checksum": v.checksum
            }
            for v in versions
        ]
    )


@app.get("/api/v1/integrity/verify", response_model=IntegrityCheckResponse)
async def verify_integrity(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_api_key)
):
    """Verify checksums of all archived events"""
    if not READ_API_ENABLED:
        raise HTTPException(status_code=404, detail="Read API not enabled")
    
    result = await db.execute(text("SELECT * FROM verify_all_checksums()"))
    row = result.fetchone()
    
    await log_access(db, "verify_integrity", request, result_count=row.total_records)
    
    return IntegrityCheckResponse(
        total_records=row.total_records,
        valid_records=row.valid_records,
        invalid_records=row.invalid_records,
        invalid_ids=[str(id) for id in row.invalid_ids] if row.invalid_ids else [],
        verified_at=datetime.now(timezone.utc).isoformat()
    )


# =============================================================================
# ADMIN ENDPOINTS
# =============================================================================

@app.post("/admin/update-github-ips")
async def trigger_github_ip_update(
    request: Request,
    x_api_key: str = Header(...)
):
    """Manually trigger GitHub IP list update"""
    if x_api_key != READ_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    await update_github_ips()
    
    return {"status": "updated", "timestamp": datetime.now(timezone.utc).isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
